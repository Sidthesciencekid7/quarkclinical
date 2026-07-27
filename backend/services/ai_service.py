"""
AI service — PK parameter extraction and protocol section generation via Claude.

Uses claude-sonnet-4-6 with tool use for structured extraction and regular
message generation for protocol sections. Each section prompt references the
exact FDA BE guidance section it maps to.
"""

import json
import math
import os

from services.biostatistics import WASHOUT_HALF_LIVES
from services.guidelines import get_profile, regulatory_directive
from services.design_decisions import design_directive as _design_decisions_directive
from typing import Any

import anthropic

MODEL = "claude-sonnet-4-6"

# Sections that legitimately run long get a larger output budget so they are
# never silently truncated mid-table or mid-list.
_DEFAULT_MAX_TOKENS = 4096
_LONG_SECTION_MAX_TOKENS = 8192
_LONG_SECTIONS = frozenset({
    "synopsis",
    "introduction",
    "study_design",
    "population",
    "treatment",
    "study_assessments",
    "safety_monitoring",
    "statistical_analysis_plan",
})

# ---------------------------------------------------------------------------
# Tool schema — forces structured PK extraction
# ---------------------------------------------------------------------------

_PK_EXTRACTION_TOOL: dict[str, Any] = {
    "name": "record_pk_parameters",
    "description": (
        "Record all pharmacokinetic parameters explicitly stated with numeric values "
        "in the provided literature abstracts or FDA label. source_pmid MUST exactly "
        "match one of the PMIDs from the input list, or be \"DAILYMED\" for values taken "
        "from the FDA label — never fabricate or approximate a PMID. "
        "Pay special attention to within-subject (intra-subject) coefficient of variation "
        "(CV%) for Cmax and for AUC in single-dose studies in healthy volunteers — these "
        "drive sample-size calculation. Record CV for Cmax as 'cv_intra_cmax' and for AUC as "
        "'cv_intra_auc' (use the generic 'cv_intra' only when the metric is unspecified). "
        "Such CV values are often reported in bioequivalence/PK-similarity studies and in "
        "regulatory review documents — if any abstract references an FDA review, EMA EPAR, "
        "Australian AUSPAR, or a Japanese (PMDA) or Chinese (NMPA) registration study with a "
        "stated CV, capture it with its population."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "parameters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "parameter": {
                            "type": "string",
                            "enum": [
                                "Cmax", "AUC0t", "AUC0inf", "Tmax",
                                "t_half", "Vd", "CL", "bioavailability", "protein_binding",
                                # Within-subject variability — the key driver of BE
                                # sample size. Report as a percentage in `value`
                                # (e.g. 24.5 for 24.5%). Prefer values from
                                # replicate-design or BE studies. Tag the metric:
                                # cv_intra_cmax (peak) vs cv_intra_auc (exposure);
                                # cv_intra/cv_within when the metric is unspecified.
                                "cv_intra", "cv_within", "cv_intra_cmax", "cv_intra_auc",
                            ],
                        },
                        "value": {"type": "number"},
                        "unit": {
                            "type": "string",
                            "description": (
                                "A SHORT standard unit ONLY, e.g. 'ng/mL', 'h', '%', 'L/h', 'L', "
                                "'ng*h/mL', or 'ratio' for a dimensionless GMR/fold-change. Max ~20 "
                                "characters. NEVER put explanatory text, study context, or "
                                "comparator/drug names here (e.g. NOT 'ratio (with/without X, "
                                "healthy subjects)') — put that context in `population` instead."
                            ),
                        },
                        "population": {
                            "type": "string",
                            "description": (
                                "e.g. 'healthy adults', 'elderly', 'renal impairment'. Any extra "
                                "context about the comparison or study design (e.g. a drug-drug "
                                "interaction ratio, or which co-administered drug) belongs here, "
                                "not in `unit`."
                            ),
                        },
                        "source_pmid": {
                            "type": "string",
                            "description": (
                                "Must exactly match one of the PMIDs provided in the literature list. "
                                "Do not invent, approximate, or combine PMIDs."
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "description": (
                                "Extraction confidence: 1.0 = value explicitly stated, "
                                "0.7–0.9 = clearly implied, <0.7 = uncertain."
                            ),
                        },
                    },
                    "required": ["parameter", "value", "unit", "source_pmid"],
                },
            }
        },
        "required": ["parameters"],
    },
}

# ---------------------------------------------------------------------------
# Section configurations
# Each entry maps a section name → FDA reference, required context keys,
# and generation instructions for Claude.
# ---------------------------------------------------------------------------

_SECTION_CONFIGS: dict[str, dict[str, Any]] = {
    "title": {
        "fda_ref": (
            "Reference clinical-trial-protocol template, Cover Page — Protocol Title "
            "and Identification"
        ),
        "context_keys": ["drug_inn", "study_type", "org_name", "guideline_version", "sample_size"],
        "instructions": (
            "Generate a formal study title following the template convention. Use the EXACT "
            "study design named in the locked design parameters (do not offer alternatives or "
            "brackets for the design). The title MUST name the assessment objectives, and for a "
            "biologic/biosimilar (study_type = 'biosimilar_pk') it MUST explicitly include "
            "Safety and Immunogenicity alongside Pharmacokinetic Similarity. Template: "
            "'A [Blinding, e.g. Double-Blind or Open-Label], Randomized, [Single-Dose/Multiple-Dose], "
            "[exact design] Study to [Evaluate/Compare] the [Bioequivalence / Pharmacokinetic "
            "Similarity], Safety[, and Immunogenicity — include for biosimilar_pk] "
            "of [Test Product] and [Reference Listed Drug (RLD)] Containing [INN] [Strength] in "
            "Healthy Adult Volunteers.' "
            "Example (biosimilar): 'A Double-Blind, Randomized, Single-Dose, Two-Arm Parallel-Group "
            "Study to Compare the Pharmacokinetic Similarity, Safety, and Immunogenicity of a "
            "Proposed Biosimilar ... and Trulicity (Dulaglutide) ... in Healthy Adult Volunteers.' "
            "For a small-molecule bioequivalence study, include Safety but omit Immunogenicity. "
            "Output only the title text — no headers, no preamble."
        ),
    },
    "synopsis": {
        "fda_ref": (
            "Reference clinical-trial-protocol template — Study Protocol Synopsis (front matter, "
            "precedes Section 1); 21 CFR 320.21 Requirements for submission of in vivo "
            "bioavailability data"
        ),
        "context_keys": [
            "drug_inn", "study_type", "pk_parameters", "population_config",
            "guideline_version", "sample_size",
        ],
        "instructions": (
            "Write the protocol synopsis as a structured summary (450-600 words) covering, "
            "with <h3> sub-headings, in this exact order: "
            "1) **Investigational Drug** — Test product and US-licensed/approved Reference product "
            "(name, strength, dosage form, route); "
            "2) **Objectives** — primary then secondary, one line each; "
            "3) **Endpoints** — primary PK endpoints, then secondary PK / safety / "
            "immunogenicity (biologics only) endpoints as a short list; "
            "4) **Study Design** — design statement, screening/treatment/follow-up periods, "
            "number of subjects planned; "
            "5) **Eligibility, Replacement and Randomisation** — one line each (full criteria "
            "appear in Population/Treatment); "
            "6) **Assessments and Evaluations** — PK, immunogenicity (if applicable) and safety "
            "assessment summary, referencing the Schedule of Assessments; "
            "7) **Statistical Methods** — sample size headline (state the exact locked N and "
            "acceptance criteria), summary of the primary analysis. "
            "For Study Design, Number of Subjects, and Acceptance Criteria you MUST use the exact "
            "values from the locked design parameters — these must match the Study Design and "
            "Statistical Analysis Plan sections verbatim. Use present tense."
        ),
    },
    "introduction": {
        "fda_ref": (
            "Reference template, Section 1 — Introduction (1.1 Background, 1.2 Product "
            "Description, 1.3 Clinical Pharmacology, 1.4 Nonclinical Studies, 1.5 Clinical "
            "Studies); 21 CFR 320.1 Definitions; 21 CFR 320.21"
        ),
        "context_keys": ["drug_inn", "brand_names", "therapeutic_class", "pk_parameters", "label_data"],
        "instructions": (
            "Write FIVE subsections using <h2> headings, in this exact order and wording: "
            "1) **Background** — the therapeutic area and unmet need the drug addresses; keep to "
            "what is directly relevant to the study context (2-3 paragraphs); "
            "2) **Product Description** — molecule type, mechanism of action, approved "
            "indications, originally-approved region/year (use label_data where available), "
            "dosage form(s) and strength(s), and (for a biosimilar) the manufacturing platform; "
            "state that for consistency the investigational product is referred to as the Test "
            "Product and the comparator as the Reference Product; "
            "3) **Clinical Pharmacology of Active Ingredient** — FOUR <h3> sub-subsections "
            "'Absorption', 'Distribution', 'Metabolism', 'Excretion', each grounded in label_data "
            "and pk_parameters where provided, with in-text citations as (PMID: XXXXXXXX) for "
            "literature-sourced values; "
            "4) **Nonclinical Studies** — one paragraph; reference that nonclinical pharmacology "
            "and toxicology are detailed in the Investigator's Brochure; "
            "5) **Clinical Studies** — summarize relevant prior Phase 1 studies of the reference "
            "or similar molecules (safety, tolerability, PK) cited from the provided literature. "
            "Formal, third-person scientific language."
        ),
    },
    "study_rationale": {
        "fda_ref": (
            "Reference template, Section 2 — Study Rationale and Risk/Benefit Assessment "
            "(2.1 Rationale for Study, 2.2 Risk/Benefit Assessment)"
        ),
        "context_keys": ["drug_inn", "study_type", "population_config", "pk_parameters", "label_data"],
        "instructions": (
            "Write TWO subsections using <h2> headings: "
            "1) **Rationale for Study** — why this study compares Test vs Reference using the "
            "locked design and the chosen dose; justify the dose from label_data/approved labeling "
            "or pk_parameters; explain why healthy subjects are used (to avoid the variability of "
            "exposure seen in patients with underlying disease/concomitant medication); if the "
            "terminal half-life (t_half in pk_parameters) is long, state that this is WHY a "
            "parallel-group design (no washout) was selected rather than a crossover; "
            "2) **Risk/Benefit Assessment** — state the risk profile is expected to be consistent "
            "with the reference product given structural/biological similarity (biosimilar) or "
            "established pharmacology (small molecule); reference that safety screening "
            "(laboratory tests, vital signs, ECGs, AE surveillance) is adequate to identify and "
            "manage risk; state explicitly that no direct therapeutic benefit is intended for the "
            "healthy subjects, and the overall risk is considered acceptable given the single-dose, "
            "well-characterized profile."
        ),
    },
    "objectives": {
        "fda_ref": (
            "Reference template, Section 3 — Study Objectives (3.1 Primary Objective, "
            "3.2 Secondary Objective); 21 CFR 320.23; 21 CFR 320.24"
        ),
        "context_keys": ["drug_inn", "study_type", "sample_size"],
        "instructions": (
            "Write TWO subsections using <h2> headings: "
            "1) **Primary Objective** — ONE sentence: to establish bioequivalence/PK similarity "
            "between Test and Reference following the locked dose/route in adult healthy subjects, "
            "based on the co-primary PK endpoints from the locked design parameters (Cmax and "
            "AUC0-inf for a single-dose study) and the acceptance criteria in those parameters; "
            "2) **Secondary Objective** — a numbered list: to evaluate the safety of Test vs "
            "Reference; to evaluate secondary PK parameters of Test vs Reference; for a biosimilar "
            "(study_type = 'biosimilar_pk') ALSO add: to evaluate the immunogenicity of Test vs "
            "Reference. "
            "Do not restate endpoints here — those belong in the Study Endpoints section."
        ),
    },
    "endpoints": {
        "fda_ref": (
            "Reference template, Section 4 — Study Endpoints (4.1 Primary Endpoint, "
            "4.2 Secondary endpoints)"
        ),
        "context_keys": ["drug_inn", "study_type", "sample_size"],
        "instructions": (
            "Write TWO subsections using <h2> headings: "
            "1) **Primary Endpoint** — a bullet list with exactly the co-primary PK endpoints from "
            "the locked design parameters. For a single-dose study these are Cmax and AUC0-inf "
            "(area under the curve from time zero extrapolated to infinity) as the co-primary "
            "exposure metrics — AUC0-inf, NOT AUC0-t, is the primary AUC endpoint; "
            "2) **Secondary endpoints** — THREE labelled sub-lists: "
            "'Pharmacokinetic Endpoints' — list AUC0-t (area under the curve to the last "
            "quantifiable concentration) FIRST as a secondary endpoint, then %AUCextrapolation, "
            "Tmax, Vz/F, CL/F, kel, t1/2; "
            "'Safety Endpoints' (incidence/severity/causality of AEs and SAEs; abnormal vital "
            "signs, 12-lead ECG, physical examination; abnormal safety laboratory investigations; "
            "for a biologic add incidence/severity of injection or infusion site reactions); "
            "for a biosimilar (study_type = 'biosimilar_pk') ALSO add 'Immunogenicity Endpoints' "
            "(incidence of anti-drug antibodies (ADA); titre and incidence of neutralizing "
            "antibodies (NAb))."
        ),
    },
    "study_design": {
        "fda_ref": (
            "Reference template, Section 5 — Study Design (5.1 Study Overview: 5.1.1 Screening "
            "Period, 5.1.2 Confinement and Check-in, 5.1.3 Study Period, 5.1.4 End of Study)"
        ),
        "context_keys": ["study_type", "population_config", "guideline_version", "sample_size", "pk_parameters"],
        "instructions": (
            "Describe the study design for the SINGLE design specified in the locked design "
            "parameters — this may be a 2-period crossover, a 2-arm parallel-group, OR a "
            "REPLICATE crossover (3-period partial-replicate TRR/RTR/RRT for a highly-variable "
            "drug, or 4-period full-replicate TRTR/RTRT for a narrow-therapeutic-index drug — "
            "check the locked design parameters for which). Do NOT present design alternatives or "
            "'Option A/Option B' menus — a finalised protocol commits to one design. Open with a "
            "short statement naming the exact design (e.g. 'Phase 1, randomized, single-center, "
            "[open-label/double-blind], [crossover / two-arm parallel-group / 3-period partial-"
            "replicate / 4-period full-replicate], single-dose, active-comparator study'), the "
            "treatment ratio (1:1, or the sequence allocation for a replicate design) and that a "
            "schematic diagram and the Schedule of Assessments (referenced as Table 1, presented "
            "in the next section) detail the procedures. "
            "Then write ONE <h2> subsection '5.1 Study Overview' containing FOUR <h3> "
            "sub-subsections in this exact order: "
            "1) **Screening Period** — eligibility screening within 28 days prior to dosing; one "
            "permitted retest at investigator discretion; "
            "2) **Confinement and Check-in** — baseline reconfirmation of eligibility, housing "
            "before dosing, randomization to a treatment SEQUENCE after eligibility is confirmed "
            "(crossover/replicate: one randomization to a 2- or 3-4-period sequence, applied once "
            "before Period 1; parallel: once, direct to Test or Reference); "
            "3) **Study Period** — route/site of administration, confinement duration post-dose "
            "for EACH period, and: for a crossover OR replicate design, the washout (>=7 x "
            "terminal t1/2, computed from the literature t1/2 in pk_parameters — for warfarin-"
            "like NTI drugs with t1/2 around 40 hours this is >=280 hours (~12 days), NOT a "
            "generic '7 days'; ALWAYS compute the actual number from the real t1/2, never state "
            "a bare '7 days' unless that specific drug's t1/2 makes 7 days genuinely >=7 "
            "half-lives) applied identically between EVERY period (not just once); OR for a "
            "parallel design state explicitly that no washout applies; "
            "4) **End of Study** — definition of the EOS visit and its assessment window "
            "(+2 days), and that early-termination subjects undergo the same EOS procedures at "
            "their last visit. "
            "Justify the chosen design from the drug's within-subject variability, narrow-"
            "therapeutic-index status, and terminal half-life in pk_parameters. Explain the "
            "trade-off explicitly: a crossover (or replicate) removes between-subject variability "
            "(each subject is their own control), so it needs FEWER subjects and is faster and "
            "more efficient and is PREFERRED when feasible; however, it requires a washout of at "
            "least 5-7 terminal half-lives between EVERY period, so for a long-half-life drug "
            "(e.g. a monoclonal antibody/fusion protein with a multi-week half-life) the washout "
            "becomes impractical (often many weeks to months) and raises immunogenicity/period-"
            "effect confounding — in that case a parallel-group design is the only feasible "
            "choice despite needing more subjects. A REPLICATE design (partial for highly-"
            "variable drugs, full for NTI drugs) is used instead of a plain 2-period crossover "
            "specifically to let the statistical analysis estimate each formulation's own within-"
            "subject variability (required for reference-scaling) — state this reason explicitly "
            "when the locked design is a replicate. State which of these applies here based on "
            "the terminal half-life and NTI/highly-variable status in pk_parameters."
        ),
    },
    "population": {
        "fda_ref": (
            "Reference template, Section 6 — Population (6.1 Subject Selection Criteria: "
            "6.1.1 Inclusion, 6.1.2 Exclusion; 6.2 Planned number of subjects; "
            "6.3 Subject Withdrawal/Discontinuation and Replacement; 6.4 Study or Study Site "
            "Termination); ICH E6(R2) Section 4.3"
        ),
        "context_keys": ["population_config", "sample_size"],
        "instructions": (
            "Write FOUR subsections using <h2> headings, in this exact order: "
            "1) **Subject Selection Criteria** — open with one sentence on the eligibility-review "
            "process, then TWO <h3> sub-subsections 'Inclusion Criteria' and 'Exclusion Criteria'. "
            "Each MUST be its OWN separate <ol>...</ol> ordered list that starts numbering again "
            "at 1 — do NOT continue the inclusion numbering into exclusion, do NOT put both lists "
            "in one <ol>, and do NOT add 'value'/'start' attributes. Inclusion MUST cover: age "
            "18-55 years inclusive (adjust per "
            "population_config), BMI 18.50-29.99 kg/m^2, healthy male/female with contraception "
            "requirements for subjects of childbearing potential, normal/not-clinically-significant "
            "clinical laboratory values, negative HIV-1/2 + HBV + HCV serology, normal/NCS "
            "physical exam + vital signs + 12-lead ECG, non-smoker status, and ability to provide "
            "informed consent. Exclusion MUST cover: inability to consent, alcohol/drug abuse or "
            "recent use, use of any investigational product within 30 days or 5 half-lives "
            "(whichever longer), recent prescription/OTC/herbal medication use, recent vaccination, "
            "significant acute or chronic illness, hypersensitivity to the drug class or "
            "formulation excipients, clinically significant abnormal vital signs at "
            "screening/check-in (give the specific BP/pulse/respiratory-rate/temperature bounds), "
            "difficult venipuncture history, and recent blood donation; "
            "2) **Planned number of subjects** — state the total N, the randomisation ratio (1:1) "
            "and the per-arm N exactly as given in the locked design parameters; "
            "3) **Subject Withdrawal/Discontinuation and Replacement** — numbered list of "
            "withdrawal circumstances (consent withdrawal; AE/safety concern; protocol violation; "
            "non-compliance; prohibited-treatment use; lost to follow-up; death; other justifiable "
            "reason), documentation requirements, and state the replacement policy: subjects "
            "withdrawn after randomization/dosing are NOT replaced; "
            "4) **Study or Study Site Termination** — the sponsor's right to terminate for medical "
            "or administrative reasons, subject notification and follow-up, and IRB notification "
            "of premature termination."
        ),
    },
    "treatment": {
        "fda_ref": (
            "Reference template, Section 7 — Treatment (7.1 Randomisation, 7.2 Investigational "
            "Products, 7.3 Management of Clinical Supplies, 7.4 Treatment Compliance, "
            "7.5 Blinding, 7.6 Prior/Concomitant/Prohibited Medications, 7.7 Subject Restrictions); "
            "21 CFR 320.25; FDA Draft Guidance on Food-Effect Bioavailability (2002)"
        ),
        "context_keys": ["drug_inn", "pk_parameters", "dose_strength", "formulation", "label_data", "study_type"],
        "instructions": (
            "Where label_data is provided, align dose/route/formulation with the approved label "
            "and name the specific Reference Listed Drug (manufacturer and strength) rather than a "
            "placeholder. If dose_strength is NOT provided in context, DO NOT hedge with a "
            "parenthetical example (e.g. NEVER write 'to match Reference (e.g., 5 mg)') — commit to "
            "ONE specific, commonly-used approved strength for this drug (using label_data/general "
            "knowledge of its approved dosage forms) and state it plainly as the strength used, since "
            "a vague placeholder-like hedge is exactly the kind of unfilled-value a regulatory "
            "reviewer will flag. Write SEVEN subsections using <h2> headings, in this exact order: "
            "1) **Randomisation/Method of Assigning Subjects to Treatment Arms** — central "
            "randomisation (e.g. IRT/IWRS) in a 1:1 ratio, randomisation-code control and "
            "concealment from blinded staff; "
            "2) **Investigational Products** — Test and Reference product identity, strength, "
            "dosage form and manufacturer; "
            "3) **Management of Clinical Supplies** — THREE <h3> sub-subsections "
            "'Study Drug Packaging, Labeling, and Storage' (label contents, storage temperature), "
            "'Study Drug Preparation and Administration' (fasting requirement, dose, route, "
            "injection/administration site and technique, post-dose fasting), "
            "'Study Drug Accountability, Dispensing, and Destruction'; "
            "4) **Treatment Compliance** — confirmation and documentation of full-dose "
            "administration; "
            "5) **Blinding** — TWO <h3> sub-subsections 'Blinding Procedures' (state the blinding "
            "level: double-blind is typical for a biosimilar PK similarity study, open-label is "
            "typical for a small-molecule BE crossover — choose based on study_type) and "
            "'Breaking the Blind' (emergency unblinding procedure, if double-blind; state N/A for "
            "an open-label design); "
            "6) **Prior, Concomitant, and Prohibited Medications** — recording window and "
            "prohibited medication classes consistent with the exclusion criteria; "
            "7) **Subject Restrictions During the Study** — meal schedule, water restriction "
            "around dosing, alcohol/recreational-drug restriction, post-dose activity restriction."
        ),
    },
    "study_assessments": {
        "fda_ref": (
            "Reference template, Section 9 — Study Assessments (9.1 Pharmacokinetic Assessment, "
            "9.2 Immunogenicity Assessments, 9.3 Blood Sample Processing, 9.4 Safety and "
            "Tolerability Assessment); FDA Guidance on Bioanalytical Method Validation (May 2018)"
        ),
        "context_keys": ["pk_parameters", "study_type", "drug_inn", "analytical_method", "lloq"],
        "instructions": (
            "Design the blood sampling schedule, deriving timing from the drug's ACTUAL "
            "pharmacokinetics in pk_parameters (cite the specific t_half/Tmax values used). "
            "Write subsections using <h2> headings, in this exact order: "
            "1) **Pharmacokinetic Assessment** — pre-dose sample within 60 minutes before dosing; "
            "at least 12-16 post-dose timepoints with 3-4 points around the literature Tmax and "
            "taper through the elimination phase so the last sample extends to >= 3-5 x terminal "
            "t1/2 (AUC0-t >= 80% of AUC0-inf); present the schedule as a Markdown table with EXACT "
            "headers '| Nominal Time (h) | Acceptable Window (min) | Volume per Draw (mL) | "
            "Cumulative Volume (mL) |' using 4-6 mL per draw; state the sample collection time "
            "windows (e.g. +-2 min confinement, +-120 min ambulatory) and total blood volume "
            "across the whole study, confirming it stays within ~500 mL per any 8-week window; "
            "2) **Immunogenicity Assessments** — ONLY if study_type is 'biosimilar_pk': ADA/NAb "
            "sampling timepoints (pre-dose plus follow-up days/weeks matching the PK tail, e.g. "
            "Day 29, 57, 85, and a Week-24 EOS sample), tiered testing strategy (screening "
            "ELISA/ECL bridging assay -> confirmatory competitive-inhibition assay -> NAb assay if "
            "positive), and sample handling. Omit this subsection entirely for non-biosimilar "
            "study types; "
            "3) **Blood Sample Processing / Bioanalytical Method** — central-laboratory analysis "
            "using a validated method; for a small molecule describe LC-MS/MS (LLOQ/ULOQ, "
            "calibration range, accuracy/precision acceptance of +-15% RE / <=15% CV, ISR >= 10% "
            "of samples); for a biologic describe a ligand-binding assay (ELISA/ECL), minimum "
            "required dilution, and +-20% RE / <=20% CV acceptance; "
            "4) **Safety and Tolerability Assessment** — SIX <h3> sub-subsections: "
            "'Demographic Data' (DOB, height, weight, BMI, sex, race); "
            "'Vital Signs' (BP/pulse/respiration/temperature at check-in, pre-dose, and defined "
            "post-dose timepoints); "
            "'Injection Site Assessment' (ONLY for a biologic/SC product — pre- and post-dose "
            "local-tolerability grading; omit for an oral small molecule); "
            "'Electrocardiogram (ECG)' (12-lead at screening, check-in, EOS); "
            "'Physical Examination' (complete at screening/EOS, abbreviated at other visits); "
            "'Adverse Event Monitoring' (non-leading, open-ended querying at each safety contact). "
            "Justify each PK sampling window based on the PK profile."
        ),
    },
    "safety_monitoring": {
        "fda_ref": (
            "Reference template, Section 10 — Safety Monitoring (10.1 Handling and Reporting of "
            "Adverse Events: Definitions; AE/SAE Reporting and Timeline; Method of Detecting; "
            "Recording and Follow-up; Abnormal Lab Results; Pregnancy Reporting; Reporting of "
            "SAEs; Concomitant Medication); ICH E6(R2) Section 4.4; 21 CFR 312.32; CTCAE v5.0"
        ),
        "context_keys": ["drug_inn", "study_type"],
        "instructions": (
            "Write ONE <h2> subsection '10.1 Handling and Reporting of Adverse Events' containing "
            "EIGHT <h3> sub-subsections in this exact order: "
            "1) **Definitions** — define, in formal regulatory language: Adverse Event (AE), "
            "Adverse Drug Reaction (ADR), Serious Adverse Event (SAE) with its full outcome "
            "criteria (death; life-threatening; hospitalization/prolongation; persistent/"
            "significant incapacity; congenital anomaly; important medical events requiring "
            "intervention to prevent those outcomes), Unexpected Adverse Event, Suspected "
            "Unexpected Serious Adverse Reaction (SUSAR), and Life-Threatening Adverse Reaction. "
            "For a biologic, also define Adverse Events of Special Interest (AESI) relevant to the "
            "molecule class (e.g. injection-site reactions, hypersensitivity/infusion reactions, "
            "infection, immunogenicity-related events); "
            "2) **AEs, SAEs, and Other Safety Reporting and Timeline** — AE collection window "
            "(informed consent through EOS), pre-dose AEs treated as such, CTCAE v5.0 and MedDRA "
            "coding; "
            "3) **Method of Detecting AEs and SAEs** — non-leading, open-ended questioning; "
            "4) **Recording and Follow-up of AEs and SAEs** — investigator follow-up "
            "responsibility through resolution; THEN explicitly cover, as their own labelled "
            "paragraphs, 'Assessment of Causality' (binary Related/Not-Related classification with "
            "alternative-cause documentation when Not-Related), 'Assessment of Expectedness' "
            "(against the Investigator's Brochure or reference safety information), and "
            "'Assessment of Severity' (CTCAE v5.0 Grade 1-5 scale, stating each grade's "
            "definition, and the distinction that 'severe' measures intensity, not seriousness); "
            "5) **Abnormal Lab Results** — criteria for reporting an abnormal result as an AE "
            "(discontinuation, intervention required, further diagnostic workup, clinically "
            "significant signs/symptoms); "
            "6) **Pregnancy Reporting** — exclusion/withdrawal of confirmed pregnancy, 24-hour "
            "sponsor notification, IRB notification within 72 hours, spontaneous abortion always "
            "treated as an SAE; "
            "7) **Reporting of Serious Adverse Events** — 24-hour sponsor notification, written "
            "report within 72 hours, and expedited regulatory timelines (7 calendar days for "
            "fatal/life-threatening SUSARs, 15 calendar days for other SUSARs, per 21 CFR 312.32); "
            "8) **Concomitant Medication and Treatment** — recording window and the "
            "investigator/sponsor decision process for permitting concomitant medication during "
            "the study."
        ),
    },
    "data_handling": {
        "fda_ref": (
            "Reference template, Section 11 — Data Handling and Record Keeping (11.1 Source "
            "documents, 11.2 Data collection, 11.3 Database Management, 11.4 Record Retention); "
            "ICH E6(R2)"
        ),
        "context_keys": ["study_type", "org_name"],
        "instructions": (
            "Write FOUR subsections using <h2> headings: "
            "1) **Source documents** — definition and examples (hospital records, lab notes, "
            "diaries, pharmacy dispensing records), site recordkeeping obligations and sponsor/"
            "IRB access for quality assurance, audits and inspection; "
            "2) **Data collection** — electronic case report form (eCRF) completion, role-based "
            "access control, investigator responsibility for completeness/accuracy/timeliness; "
            "3) **Database Management** — query resolution process, medical coding dictionary use "
            "for AEs/medical history, database lock procedure requiring joint sponsor/CRO "
            "authorization for any post-lock changes; "
            "4) **Record Retention** — retention period (state 25 years or per applicable "
            "regulatory guideline, whichever is longer, after study completion or regulatory "
            "submission) and that records are never destroyed without written sponsor approval."
        ),
    },
    "study_monitoring": {
        "fda_ref": (
            "Reference template, Section 12 — Study Monitoring, Auditing, and Inspecting "
            "(12.1 Responsibility of investigators, 12.2 Responsibilities of sponsor, "
            "12.3 Auditing and inspecting, 12.4 Quality control and quality assurance); "
            "ICH E6(R2)"
        ),
        "context_keys": ["study_type", "org_name"],
        "instructions": (
            "Write FOUR subsections using <h2> headings: "
            "1) **Responsibility of investigators** — compliance with the protocol and ICH-GCP, "
            "providing reliable data, and direct access to source documents for the sponsor; "
            "2) **Responsibilities of sponsor and study management plan** — sponsor's role in "
            "ensuring ethical/scientific/regulatory quality, risk assessment and mitigation, and "
            "clinical monitoring; "
            "3) **Auditing and inspecting** — investigator's obligation to permit monitoring, "
            "audits and regulatory inspections with direct access to study records, and "
            "confidentiality obligations of auditors/inspectors; "
            "4) **Quality control and quality assurance** — verification that data are authentic, "
            "accurate and complete, that participant rights/safety are protected, and that the "
            "study is conducted per the approved protocol, amendments and applicable regulations."
        ),
    },
    "statistical_analysis_plan": {
        "fda_ref": (
            "Reference template, Section 13 — Statistical Methods and Data Analysis (13.1 Study "
            "Populations; 13.2 Pharmacokinetic Analyses; 13.3 Immunogenicity Analyses; 13.4 Safety "
            "Analyses; 13.5 Sample Size Determination; 13.6 Summary Statistics; 13.7 ANOVA; "
            "13.8 Confidence Intervals and Ratio Analysis; 13.9 Inter-Subject Variability; "
            "13.10 Power; 13.11 Bioequivalence Criteria; 13.12 Outlier Detection; 13.13 Interim "
            "analyses; 13.14 Blinded Sample Size Re-estimation); FDA Statistical Approaches to "
            "Establishing Bioequivalence (2001); 21 CFR 320.23"
        ),
        "context_keys": ["pk_parameters", "study_type", "acceptance_criteria", "cv_intra", "sample_size"],
        "instructions": (
            "Write the complete statistical analysis plan, tailored to study_type, as ELEVEN <h2> "
            "subsections in this exact order: "
            "1) **Study Populations** — define the Randomised Set, Pharmacokinetic Set (dosed with "
            "reliable bioanalytical data; BLQ concentrations treated as zero), and Safety Set "
            "(received any dose, analysed as treated); briefly note disposition-of-subjects and "
            "protocol-deviation reporting will summarise enrolment/completion by treatment arm; "
            "2) **Pharmacokinetic Analyses** — NCA derivation (Cmax, Tmax observed; AUC0-t "
            "linear-log trapezoidal; AUC0-inf = AUC0-t + Clast/kel; kel by terminal log-linear "
            "regression with R^2 >= 0.7; t1/2 = ln(2)/kel); natural-log transform of Cmax and AUC "
            "before analysis; for the PRIMARY analysis the co-primary endpoints are Cmax and "
            "AUC0-inf (AUC0-t is a supportive secondary endpoint analysed identically): an "
            "ANCOVA/mixed-effects model with treatment as a fixed effect (state the locked design's "
            "effects/covariates) and the EXACT acceptance limits from the locked design parameters "
            "for the 90% CI of the geometric mean ratio; "
            "3) **Handling of Missing Data, Withdrawals, and Discontinuations** — specify: (a) BLQ "
            "concentrations are set to zero before first quantifiable concentration and treated as "
            "missing thereafter; (b) a subject's PK parameter is excluded from the PK Set if the "
            "concentration-time profile is inadequate to estimate it reliably — in particular, if "
            "3 or more CONSECUTIVE scheduled PK timepoints are missing, or missing samples preclude "
            "reliable estimation of Cmax or of the terminal slope (so AUC0-inf cannot be derived "
            "with %AUCextrap <= 20%), that parameter (or the subject) is excluded from the primary "
            "PK analysis; (c) a subject who DISCONTINUES due to an adverse event is followed for "
            "safety, remains in the Safety Set, and contributes available PK data to the PK Set "
            "only if a valid primary parameter can still be derived; (d) a subject who discontinues "
            "for NON-COMPLIANCE (dosing error, protocol deviation affecting PK, emesis within a "
            "pre-specified window for oral products, or lost to follow-up) is excluded from the "
            "Per-Protocol/PK Set; (e) subjects withdrawn or discontinued after dosing are NOT "
            "replaced; sensitivity analyses with and without excluded subjects will be reported; "
            "4) **Immunogenicity Analyses** — ONLY if study_type is 'biosimilar_pk': descriptive "
            "ADA/NAb incidence, titre, and an exploratory comparison of PK between ADA-positive and "
            "ADA-negative subjects. Omit entirely for non-biosimilar study types; "
            "5) **Safety Analyses** — descriptive summary (comparative summary, individual "
            "listings) of AEs/SAEs (MedDRA-coded, by System Organ Class and Preferred Term), "
            "laboratory tests, vital signs, ECG, physical examination, and (for a biologic) "
            "injection/infusion-site reactions and concomitant medications; "
            "6) **Sample Size Determination** — the within-subject CV used to power the study is the "
            "HIGHEST within-subject CV observed for either Cmax or AUC across published "
            "single-dose PK studies in the SAME population (healthy volunteers), taken as a "
            "conservative basis. If the locked design parameters include a 'cv_table' (a list of "
            "trials with their source, population, parameter, and CV%), render it as an HTML "
            "<table> with columns Study/Source | Population | Parameter | Within-subject CV% and a "
            "final row identifying the maximum CV adopted; explicitly state which study and which "
            "parameter (Cmax vs AUC) gave the highest CV. State the assumed GMR, target power, and "
            "significance level EXACTLY as given in the locked design parameters, the planned "
            "enrolment (with dropout allowance) and the resulting evaluable N. State simply that the "
            "sample size was calculated in R using the two one-sided tests (TOST) method — do NOT "
            "reproduce the calculation, formulas, or console output. Do NOT recompute or "
            "restate a different sample size — cite the planned N exactly as given. If the CV is "
            "marked 'assumed' (no literature value was available), say so and note that values from "
            "FDA/EMA reviews, AUSPAR, and Japanese (PMDA) or Chinese (NMPA) registration studies "
            "should be sought to confirm it; "
            "7) **Summary Statistics** — descriptive statistics (n, mean, SD, median, min, max, "
            "geometric mean, CV%) for demographics and all PK parameters, plus geometric means, "
            "ratio of means, and the 90% CI for the primary PK parameters; "
            "8) **Analysis of Variance (ANOVA)** — the mixed-effects/ANOVA model on log-transformed "
            "Cmax and AUC, least-squares means, and the two-one-sided-tests (TOST) procedure at "
            "alpha = 0.05; "
            "9) **Confidence Intervals (CI) and Ratio Analysis** — derivation of the 90% CI for "
            "the Test/Reference ratio of geometric least-squares means by exponentiating the CI of "
            "the log-scale difference, plus untransformed per-subject ratio statistics; "
            "10) **Inter-Subject Variability, Power, Bioequivalence Criteria, Outlier Detection** — "
            "four short paragraphs covering: inter-subject variability will be reported; power is "
            "the post-hoc probability of detecting the assumed difference at alpha = 0.05; "
            "bioequivalence/similarity is concluded if the 90% CI for the co-primary endpoints "
            "Cmax and AUC0-inf falls EXACTLY within the locked acceptance limits; "
            "outliers are identified by a studentized-residual or similar method and analyses are "
            "reported both including and excluding them; "
            "11) **Interim Analyses and Blinded Sample Size Re-estimation** — state whether an "
            "interim analysis is planned (default: none); if the locked design flags "
            "needs_simulation OR a moderate/assumed CV, describe an optional Blinded Sample Size "
            "Re-estimation (BSSR) after ~50% of subjects complete dosing, with a pre-specified "
            "alpha-adjustment method to preserve the overall Type I error rate, full details "
            "deferred to the SAP. "
            "State explicitly: all primary analyses are pre-specified in this plan; no post-hoc "
            "changes to primary endpoints or acceptance criteria are permitted."
        ),
    },
    "ethics": {
        "fda_ref": (
            "Reference template, Section 14 — Ethical and Regulatory Standards (14.1 Ethical "
            "principles, laws, and regulations; 14.2 Ethics Committee/IRB; 14.3 Informed Consent "
            "Process; 14.4 Participant and Data Confidentiality); ICH E6(R2); "
            "21 CFR Part 50; 21 CFR Part 56; Declaration of Helsinki (2013 revision); "
            "21 CFR Part 11"
        ),
        "context_keys": ["org_name", "irb_info", "study_type"],
        "instructions": (
            "Write FOUR subsections using <h2> headings, in this exact order: "
            "1) **Ethical principles, laws, and regulations** — conduct per the Declaration of "
            "Helsinki, ICH E6(R2) GCP, and applicable local regulatory requirements (21 CFR "
            "Parts 50/56 for US sites); "
            "2) **Ethics Committee/Institutional Review Board** — protocol and consent-material "
            "submission and written approval required before enrolment; amendments submitted "
            "before implementation (except to eliminate immediate hazard); annual progress "
            "reporting; "
            "3) **Informed Consent Process** — written consent per 21 CFR 50.25 required elements, "
            "obtained before any study procedure, in language the subject understands, with a "
            "copy provided to the subject, who may withdraw at any time; "
            "4) **Participant and Data Confidentiality** — coded subject identifiers, no PHI in "
            "data transmissions, sponsor/monitor/IRB access to records for quality assurance, and "
            "21 CFR Part 11 compliance for electronic records and signatures (audit-trailed, "
            "access-controlled, tamper-evident)."
        ),
    },
    "administrative": {
        "fda_ref": (
            "Reference template, Section 15 — Administrative Procedures (15.1 Protocol "
            "amendments; 15.2 Protocol deviations and violations; 15.3 Insurance; 15.4 Indemnity "
            "agreement and study finances; 15.5 Property rights and data protection; 15.6 Clinical "
            "study report; 15.7 Publication policy)"
        ),
        "context_keys": ["study_type", "org_name"],
        "instructions": (
            "Write SEVEN subsections using <h2> headings, in this exact order: "
            "1) **Protocol amendments** — written amendments require sponsor, regulatory authority "
            "(where applicable) and IRB approval before implementation, except immediate "
            "safety-driven changes (IRB informed promptly); "
            "2) **Protocol deviations and violations** — definition, prompt site reporting "
            "obligation, and corrective-action requirement; "
            "3) **Insurance** — sponsor-arranged liability insurance covering the trial, in place "
            "before first enrolment; "
            "4) **Indemnity agreement and study finances** — coverage scope, and that deviations "
            "from the protocol (e.g. unauthorized dose/route changes) are not covered; "
            "5) **Property rights and data protection** — all study results/data/inventions are "
            "the sponsor's exclusive property; one <h3> sub-subsection 'Data Protection and GDPR "
            "Compliance' covering safeguards against unauthorized third-party access; "
            "6) **Clinical study report** — a CSR will be prepared per the ICH E3 guideline "
            "regardless of completion status, in eCTD/CDISC format; "
            "7) **Publication policy** — trial registration, sponsor's right to publish, and that "
            "investigators will not publish independently without prior written sponsor approval."
        ),
    },
    "references": {
        "fda_ref": (
            "Vancouver citation style (ICMJE recommendations, 2023 update); reference template "
            "Section 16 — References"
        ),
        "context_keys": ["drug_inn", "pk_parameters", "study_type"],
        "instructions": (
            "Generate a references section in Vancouver/ICMJE format. "
            "If study_type is 'BE' (small molecule), include these mandatory references: "
            "1) FDA Guidance on BA/BE Studies in NDAs/INDs (March 2014); "
            "2) FDA Statistical Approaches to Establishing Bioequivalence (January 2001); "
            "3) FDA Guidance on Bioequivalence Studies with PK Endpoints for ANDAs (December 2013); "
            "4) FDA Bioanalytical Method Validation Guidance (May 2018); "
            "5) ICH E6(R2) Good Clinical Practice; 6) 21 CFR Part 320. "
            "If study_type is 'biosimilar_pk' (biologic), instead include: "
            "1) FDA Guidance: Scientific Considerations in Demonstrating Biosimilarity (2015); "
            "2) FDA Guidance: Immunogenicity Assessment for Therapeutic Protein Products (2014); "
            "3) FDA Guidance: Assay Development for Immunogenicity Testing (2019); "
            "4) FDA Guidance: Clinical Pharmacology Data to Support Biosimilarity (2016); "
            "5) ICH E6(R2) Good Clinical Practice; "
            "6) FDA Guidance: Bioanalytical Method Validation (May 2018); "
            "7) EMA Biosimilar Guideline CHMP/437/04. "
            "In both cases add: any PK literature (use source_pmid values from context). "
            "Format: [n] Author A, Author B. Title. Journal. Year;Vol(Issue):Pages. PMID: XXXXXXXX. "
            "Number sequentially in order of citation."
        ),
    },
}

# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class AIService:
    def __init__(self) -> None:
        # max_retries above the default (2) so the concurrent burst of section
        # calls rides out transient rate-limit (429) / overloaded (529) responses
        # with exponential backoff. Does not retry billing/auth errors (4xx).
        self._client = anthropic.AsyncAnthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            max_retries=4,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract_pk_parameters(
        self, literature: list[dict], drug_inn: str, label_text: str | None = None
    ) -> list[dict]:
        """Extract PK parameters from literature abstracts and the FDA label.

        Uses tool_choice forcing so Claude always calls record_pk_parameters.
        Every returned parameter is guaranteed to have a source_pmid that matches
        a PMID from the input literature list, or "DAILYMED" for values taken from
        the FDA-approved label — which reliably reports terminal half-life, Tmax
        and Cmax that PubMed abstracts often omit. This materially raises the yield
        of t_half / Tmax / CV, which drive washout, sampling and sample size.

        Set MOCK_AI=1 to skip the API call and return no parameters, so the full
        pipeline (sample size via CV-assumption, sections, export) runs without
        spending credits — mirrors the stub in generate_protocol_section().
        """
        if os.environ.get("MOCK_AI"):
            return []

        valid_pmids = {
            str(item["pmid"])
            for item in literature
            if item.get("pmid")
        }

        docs = list(literature)
        if label_text and label_text.strip():
            docs.append({
                "pmid": "DAILYMED",
                "title": f"FDA-approved label (DailyMed) for {drug_inn}",
                "abstract": label_text.strip(),
            })
            valid_pmids.add("DAILYMED")

        if not docs:
            return []

        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=4096,
            tools=[_PK_EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "record_pk_parameters"},
            messages=[
                {
                    "role": "user",
                    "content": _build_extraction_prompt(docs, drug_inn),
                }
            ],
        )

        raw = _unpack_tool_result(response)
        return _validate_pmids(raw, valid_pmids)

    async def generate_protocol_section(
        self, section_name: str, context: dict
    ) -> str:
        """Generate one protocol section with only the context it needs.

        Each section prompt specifies the FDA BE guidance section it maps to.
        Set MOCK_AI=1 in the environment to return stubs without API calls.
        """
        if os.environ.get("MOCK_AI"):
            drug = context.get("drug_inn", "Drug")
            study = context.get("study_type", "BE")
            ss = context.get("sample_size") or {}
            n = ss.get("n_enrolled") or ss.get("n_total") or "TBD"
            title = section_name.replace("_", " ").title()
            # The title section is a clean one-line study title (used as the cover
            # subtitle), so return that shape rather than the rich placeholder.
            if section_name == "title":
                return (
                    f"A Single-Dose, Randomized, Two-Treatment Study to Evaluate the "
                    f"Bioequivalence of Test and Reference {drug.title()} in Healthy "
                    f"Adult Volunteers (MOCK SAMPLE)."
                )
            # Representative structured HTML (paragraph + sub-heading + list +
            # table) so a MOCK export demonstrates the EXACT body formatting the
            # real document uses — only the prose is placeholder, not the layout.
            return (
                f"<p><strong>[MOCK PLACEHOLDER — {title}]</strong> This is sample text "
                f"that demonstrates document formatting only (MOCK_AI=1, no API spend). "
                f"Study drug: {drug}; study type: {study}; planned enrolment N = {n}. "
                f"Real Claude-generated content replaces this when MOCK_AI is unset.</p>"
                f"<h3>Representative Subsection</h3>"
                f"<p>Sub-headings, numbered lists and tables render here exactly as they "
                f"will in the final document — identical fonts, spacing and numbering.</p>"
                f"<ul><li>First representative point for this section.</li>"
                f"<li>Second representative point for this section.</li></ul>"
                f"<table><thead><tr><th>Parameter</th><th>Example Value</th><th>Unit</th></tr>"
                f"</thead><tbody><tr><td>Sample parameter A</td><td>12.3</td><td>h</td></tr>"
                f"<tr><td>Sample parameter B</td><td>4.56</td><td>ng/mL</td></tr></tbody></table>"
            )

        if section_name not in _SECTION_CONFIGS:
            valid = sorted(_SECTION_CONFIGS)
            raise ValueError(
                f"Unknown section '{section_name}'. Valid: {valid}"
            )

        config = _SECTION_CONFIGS[section_name]
        filtered = _filter_context(context, config["context_keys"])
        system_prompt, user_prompt = _build_section_prompt(
            section_name, config, filtered
        )

        max_tokens = (
            _LONG_SECTION_MAX_TOKENS
            if section_name in _LONG_SECTIONS
            else _DEFAULT_MAX_TOKENS
        )

        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()

        # If the model hit the token ceiling, request one continuation and
        # stitch it on so the section is never delivered half-finished.
        if response.stop_reason == "max_tokens" and text:
            try:
                cont = await self._client.messages.create(
                    model=MODEL,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[
                        {"role": "user", "content": user_prompt},
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": "Continue exactly where you stopped; do not repeat earlier text."},
                    ],
                )
                cont_text = "".join(
                    b.text for b in cont.content if b.type == "text"
                ).strip()
                if cont_text:
                    text = f"{text} {cont_text}".strip()
            except Exception:
                pass  # keep the partial section rather than failing the export

        return text


# ---------------------------------------------------------------------------
# Pure helpers (no I/O — straightforward to unit-test)
# ---------------------------------------------------------------------------


def _build_extraction_prompt(literature: list[dict], drug_inn: str) -> str:
    blocks = []
    for item in literature:
        pmid = item.get("pmid", "UNKNOWN")
        title = item.get("title", "(no title)")
        abstract = item.get("abstract", "(no abstract)")
        blocks.append(f"PMID: {pmid}\nTitle: {title}\nAbstract: {abstract}")

    joined = "\n\n---\n\n".join(blocks)
    return (
        f"You are a clinical pharmacology specialist. "
        f"Extract all PK parameters for {drug_inn} from the {len(literature)} abstracts below.\n\n"
        "STRICT RULES:\n"
        "1. source_pmid must be an exact match to one of the PMIDs listed above.\n"
        "2. Extract only values explicitly stated as numbers — no estimates or inferences.\n"
        "3. If a value appears in multiple studies, create one entry per study.\n"
        "4. Set confidence = 1.0 for clearly stated values, lower for high-variability reports.\n"
        "5. IMPORTANT: if any abstract reports the within-subject (intra-subject) "
        "coefficient of variation — often from a replicate-design or bioequivalence "
        "study, sometimes labelled CVw, intra-subject CV, or ISCV — record it as "
        "parameter 'cv_intra' with the percentage in `value` (e.g. 24.5). This single "
        "number drives the BE sample size, so capture it whenever present.\n"
        "6. The FDA-approved label may appear as the document with PMID 'DAILYMED'. "
        "Its Clinical Pharmacology / Pharmacokinetics section reliably reports terminal "
        "half-life (t_half), Tmax, Cmax, AUC, volume of distribution, clearance, protein "
        "binding and bioavailability — extract every value stated there, using "
        "source_pmid 'DAILYMED'.\n"
        "7. Normalize time-based parameters (t_half, Tmax) to HOURS (days×24, minutes÷60) "
        "and put 'h' in `unit`.\n\n"
        f"Literature:\n\n{joined}\n\n"
        "Call record_pk_parameters with all parameters you can identify."
    )


def _build_section_prompt(
    section_name: str, config: dict[str, Any], context: dict
) -> tuple[str, str]:
    section_title = section_name.replace("_", " ").title()
    context_block = (
        json.dumps(context, indent=2, default=str) if context else "{}"
    )

    study_type = context.get("study_type", "BE")
    guideline_version = context.get("guideline_version")
    system_prompt, protocol_type = _expertise_for(study_type, guideline_version)

    design_directive = _locked_design_directive(context.get("sample_size"))
    reg_directive = regulatory_directive(guideline_version or "", study_type)
    decisions_directive = _design_decisions_directive(context.get("design_decisions") or [])

    user_prompt = (
        f"Generate the '{section_title}' section for a {protocol_type} study protocol.\n\n"
        f"Regulatory Reference:\n{config['fda_ref']}\n"
        f"{reg_directive}\n"
        f"{decisions_directive}\n"
        f"Instructions:\n{config['instructions']}\n"
        f"{design_directive}\n"
        f"Context:\n{context_block}\n\n"
        "Write only the section body text."
    )

    return system_prompt, user_prompt


def _expertise_for(study_type: str, guideline_version: str | None = None) -> tuple[str, str]:
    """(system-prompt expertise framing, protocol-type label) for a study type.

    Shared by the per-section path (_build_section_prompt) and the three-stage
    pipeline's single combined-generation call (services/three_stage_pipeline.py)
    so both produce text in the same voice regardless of how many API calls it
    takes to write the document.

    ``guideline_version`` (when given) swaps the regulator/region framing so an
    EMA or ICH M13 protocol is written to the right authority instead of a
    hardcoded FDA voice. The biosimilar path stays FDA-351(k)-specific because
    this app's biosimilar immunogenicity pathway is modeled on FDA guidance.
    """
    if study_type == "biosimilar_pk":
        expertise = (
            "You are a senior clinical pharmacologist and regulatory writer specializing in "
            "FDA biosimilar development under the 351(k) pathway (Biologics Price Competition "
            "and Innovation Act), FDA Biosimilar Guidance (2019), ICH E6(R2) GCP, and "
            "21 CFR Part 11. You have deep expertise in immunogenicity assessment, ligand "
            "binding assays, and PK similarity evaluation for biological products. "
            "You produce audit-ready, regulatory-submission-quality text. "
            "Write in formal third-person scientific language suitable for FDA biosimilar review. "
            "Output only the section body — no headings, numbering, or meta-commentary."
        )
        return expertise, "biosimilar PK similarity"

    prof = get_profile(guideline_version or "")
    expertise = (
        f"You are a senior clinical pharmacologist and regulatory writer specializing in "
        f"bioequivalence protocol development for the {prof.regulator} ({prof.region}), "
        f"working to {prof.citation}, ICH E6(R2) GCP, and 21 CFR Part 11. You produce "
        f"audit-ready, regulatory-submission-quality text for small molecule chemical drug "
        f"products. Refer to the comparator as the {prof.reference_product_term}. "
        f"Write in formal third-person scientific language suitable for {prof.regulator} "
        f"review. Output only the section body — no headings, numbering, or meta-commentary."
    )
    return expertise, "bioequivalence"


def _locked_design_directive(ss: dict[str, Any] | None) -> str:
    """Authoritative design block injected into every section that receives the
    sample_size recommendation, so the design, N, and limits are identical across
    the synopsis, study design, objectives, subject selection and SAP."""
    if not ss:
        return ""
    per_arm = (
        f" ({ss['n_per_group']} per arm)"
        if ss.get("n_per_group")
        else ""
    )
    lines = [
        "\nLOCKED DESIGN PARAMETERS — use these EXACT values; do NOT invent or",
        "recalculate the design, sample size, or acceptance limits:",
        f"- Study design: {ss['design']}",
        f"- Statistical approach: {ss['approach']}",
        f"- Acceptance limits: {ss['lower_limit']*100:.2f}-{ss['upper_limit']*100:.2f}% "
        "for the 90% CI of the Test/Reference geometric mean ratio",
        f"- Co-primary endpoints: {ss['endpoint']}",
        f"- Assumed within-subject CV: {ss['cv_pct']}% (source: {ss['cv_source']})",
        f"- True GMR assumption: {ss['gmr']:.2f}; Power: {ss['power']*100:.0f}%; "
        f"alpha: {ss['alpha']}",
        f"- Planned enrolment: {ss['n_enrolled']} subjects{per_arm}, "
        f"yielding {ss['n_evaluable']} evaluable "
        f"({ss['dropout_rate']*100:.0f}% dropout allowance)",
    ]
    # Washout is computed HERE (once) and locked, rather than left for the model
    # to (re)derive independently in the synopsis AND study design — those two
    # sections are written in the same combined call but were observed to state
    # different washout durations for the same drug (e.g. synopsis said a
    # generic "7 days" for warfarin while the real requirement, 7 x its ~40h
    # half-life, is ~12 days) — a real Stage-3-caught inconsistency.
    design_key = ss.get("design_key")
    t_half = ss.get("t_half_hours")
    if design_key == "parallel":
        lines.append("- Washout: NOT APPLICABLE (parallel-group design; state this explicitly).")
    elif t_half:
        washout_hours = WASHOUT_HALF_LIVES * t_half
        if washout_hours < 24.0:
            # A sub-day figure like "0.6 days" is never how a real protocol
            # would phrase a washout — state it in whole hours instead (a
            # clinical-scheduling-realistic unit for short-half-life drugs).
            washout_str = f">= {math.ceil(washout_hours)} hours"
        else:
            washout_str = f">= {washout_hours / 24.0:.1f} days"
        lines.append(
            f"- Washout between EVERY period: {washout_str} "
            f"({WASHOUT_HALF_LIVES:.0f} x t1/2, t1/2={t_half:.1f}h) — use this EXACT "
            f"figure everywhere washout is mentioned (synopsis, study design); never "
            f"state a generic '7 days' unless it happens to equal this value."
        )
    if ss.get("needs_simulation"):
        lines.append(
            "- NOTE: this scaled/replicate method's final N must be confirmed by "
            "simulation; present the stated N as the planned/conservative estimate."
        )
    return "\n".join(lines) + "\n"


def _filter_context(context: dict, keys: list[str]) -> dict:
    """Return only the subset of context keys a given section requires."""
    return {k: context[k] for k in keys if k in context}


def _unpack_tool_result(response) -> list[dict]:
    """Extract the parameters list from a tool_use response block."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "record_pk_parameters":
            return block.input.get("parameters", [])
    return []


def _validate_pmids(parameters: list[dict], valid_pmids: set[str]) -> list[dict]:
    """Drop any parameter whose source_pmid is not in the input literature."""
    return [
        p for p in parameters
        if str(p.get("source_pmid", "")) in valid_pmids
    ]
