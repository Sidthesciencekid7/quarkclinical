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
from services.guidelines import (
    get_profile,
    governing_citation,
    reference_product_term,
    regulatory_directive,
)
from services.design_decisions import design_directive as _design_decisions_directive

# Deterministic engines. Every number a regulator could challenge is computed in
# Python by one of these modules and injected here as a LOCKED fact the model may
# only narrate. Each engine exposes a *_directive() that renders its own result
# under an "ALREADY DETERMINED" header and returns "" for empty/missing input, so
# the assembly in _engine_directives() can concatenate them unconditionally.
from services.bioanalytical import bioanalytical_directive
from services.crf_dmp import crf_dmp_directive
from services.ddi_screening import ddi_directive
from services.icf import icf_directive
from services.comparative_safety import comparative_safety_directive
from services.meta_analysis import meta_analysis_directive
from services.multiplicity import multiplicity_directive
from services.patient_population import patient_population_directive
from services.trial_precedent import trial_precedent_directive
from services.randomization import randomization_directive
from services.safety_gating import safety_gating_directive
from services.safety_reporting import safety_reporting_directive
from services.sampling_schedule import sampling_schedule_directive
from services.stratification import stratification_directive
from services.literature_service import regulatory_sources_directive, regulatory_extraction_hint

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
    # Data Handling now carries the computed CRF specification and the Data
    # Management Plan (services/crf_dmp.py), which include tables; the default
    # 4096-token budget truncated it mid-table.
    "data_handling",
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
        "stated CV, capture it with its population. "
        "MEAN +/- SD IS AS IMPORTANT AS A STATED CV%: many sources — this is the norm for "
        "biologics/monoclonal-antibody labels and first-in-human papers, which report "
        "'mean (+/- SD) Cmax was X +/- Y' far more often than a percentage CV — give a mean "
        "and a standard deviation for Cmax/AUC0t/AUC0inf WITHOUT ever stating a percentage "
        "CV. When that happens, DO NOT skip the entry and do NOT compute a percentage "
        "yourself: record the mean in `value` on the normal Cmax/AUC0t/AUC0inf row AND put "
        "the paired standard deviation in that SAME row's `sd` field. The app derives CV% "
        "from mean/SD deterministically in Python — your job is only to transcribe both "
        "numbers faithfully, never to do that division yourself."
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
                        "sd": {
                            "type": "number",
                            "description": (
                                "The standard deviation paired with `value`, ONLY for a "
                                "Cmax/AUC0t/AUC0inf row whose source reports mean +/- SD and does "
                                "NOT separately state a percentage CV. Same unit as `value` (do not "
                                "convert). Leave unset for every other row, including one whose "
                                "source already gives an explicit CV% (do not populate `sd` there — "
                                "the explicit CV wins and `sd` would be redundant). Never invent an "
                                "SD that is not stated."
                            ),
                        },
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
            "of [Test Product] and [Reference Product - name it with the term the REGULATORY "
            "FRAMEWORK block gives for this submission: the Reference Listed Drug (RLD) for a "
            "small-molecule bioequivalence study, the US-licensed reference product for a "
            "biosimilar (study_type = 'biosimilar_pk'), never both] Containing [INN] [Strength] "
            "in Healthy Adult Volunteers.' "
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
            "guideline_version", "sample_size", "sampling_schedule",
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
            "Statistical Analysis Plan sections verbatim. Use present tense. "
            "In part 6 (Assessments and Evaluations), when a `sampling_schedule` object is "
            "present in context or a LOCKED PK BLOOD-SAMPLING SCHEDULE block is supplied, "
            "summarise it using its EXACT locked numbers — the number of PK samples per "
            "period, the first and last nominal timepoint, and the total blood volume across "
            "the study — and state that the full timepoint list appears in Study Assessments. "
            "Do NOT re-derive, round or invent a different sample count, last timepoint or "
            "blood volume here; those figures must match Study Assessments verbatim. If no "
            "sampling schedule is supplied, describe the PK sampling qualitatively without "
            "quoting specific timepoint counts or volumes. "
            "In part 7 (Statistical Methods), whenever you quote the CV used to power the "
            "study, use the correct term for the locked design: 'within-subject CV' for a "
            "crossover or replicate design, or 'between-subject CV' for a parallel design (a "
            "parallel design has no within-subject comparison) — read the design from the "
            "locked design parameters and do not default to 'within-subject' unconditionally. "
            "Its provenance MUST match the CV PROVENANCE line in the locked "
            "design parameters: if that line says the CV is INVESTIGATOR-SPECIFIED, write that "
            "it was specified by the investigator/sponsor and do NOT describe it as literature-"
            "derived, pooled from published studies, or supported by a table of prior trials; "
            "if it says the CV is ASSUMED, say so plainly; otherwise describe it as sourced "
            "from the published literature exactly as the locked parameters state."
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
        "context_keys": [
            "drug_inn", "study_type", "population_config", "pk_parameters", "label_data",
            "regulatory_sources",
        ],
        "instructions": (
            "Write TWO subsections using <h2> headings: "
            "1) **Rationale for Study** — why this study compares Test vs Reference using the "
            "locked design and the chosen dose; justify the dose from label_data/approved labeling "
            "or pk_parameters; explain why healthy subjects are used (to avoid the variability of "
            "exposure seen in patients with underlying disease/concomitant medication); if the "
            "terminal half-life (t_half in pk_parameters) is long, state that this is WHY a "
            "parallel-group design (no washout) was selected rather than a crossover. WHEN a "
            "`regulatory_sources` object or LOCKED FACTS - REGULATORY ASSESSMENT-REPORT EVIDENCE "
            "block is supplied and lists a referenced source, you MAY cite it in support of the "
            "dose/PK rationale — attributed to the PMID of the retrieved paper that reports it "
            "(e.g. 'PMID <id> reports data from the EMA EPAR'), never phrased as though this "
            "protocol consulted the assessment report itself. If no source is referenced, do not "
            "mention any regulatory assessment report; "
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
        "context_keys": ["drug_inn", "study_type", "sample_size", "multiplicity"],
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
        "context_keys": ["study_type", "population_config", "guideline_version", "sample_size", "pk_parameters", "patient_population", "comparative_safety"],
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
            "Justify the chosen design from the drug's terminal half-life, narrow-therapeutic-"
            "index status, and — for a crossover or replicate design — its within-subject "
            "variability; for a PARALLEL design, state plainly that there is no within-subject "
            "comparison at all and the design is instead powered on the drug's between-subject "
            "variability, which is why it needs more subjects. Explain the "
            "trade-off explicitly: a crossover (or replicate) removes between-subject variability "
            "(each subject is their own control), so it needs FEWER subjects and is faster and "
            "more efficient and is PREFERRED when feasible; however, it requires a washout of at "
            "least 5-7 terminal half-lives between EVERY period, so for a long-half-life drug "
            "(e.g. a monoclonal antibody/fusion protein with a multi-week half-life) the washout "
            "becomes impractical (often many weeks to months) and raises immunogenicity/period-"
            "effect confounding — in that case a parallel-group design is the only feasible "
            "choice despite needing more subjects, because it must instead be powered on "
            "between-subject variability. A REPLICATE design (partial for highly-"
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
        "context_keys": [
            "population_config", "sample_size", "ddi", "safety_gating", "stratification",
            "randomization", "patient_population", "trial_precedent",
        ],
        "instructions": (
            "Write FOUR subsections using <h2> headings, in this exact order: "
            "1) **Subject Selection Criteria** — open with one sentence on the eligibility-review "
            "process, then TWO <h3> sub-subsections 'Inclusion Criteria' and 'Exclusion Criteria'. "
            "Each MUST be its OWN separate <ol>...</ol> ordered list that starts numbering again "
            "at 1 — do NOT continue the inclusion numbering into exclusion, do NOT put both lists "
            "in one <ol>, and do NOT add 'value'/'start' attributes. "
            "WHEN a `trial_precedent` object is present in context or a LOCKED INCLUSION/EXCLUSION "
            "CRITERIA PRECEDENT block is supplied AND real prior-trial precedent was found (not the "
            "'NOT FOUND' variant of that block), the Inclusion Criteria and Exclusion Criteria lists "
            "MUST be primarily grounded in that real precedent — draw specific criteria from the "
            "cited trials, adapted to this study's own design (dosing, endpoints, comparator), and "
            "cite the NCT number for a criterion drawn from a specific trial. The generic template "
            "below (age/BMI bounds, contraception, standard exclusions) is what to fall back to for "
            "any criterion the precedent trials do not address (e.g. this study's own washout/"
            "concomitant-medication and safety-screening requirements), not a replacement for the "
            "precedent-grounded criteria. When the LOCKED block instead states no real precedent was "
            "found (or no `trial_precedent` object is present at all), use the generic template below "
            "exactly as given, and if the 'NOT FOUND' block is present, state its disclosure sentence "
            "plainly rather than silently falling back with no explanation. Inclusion MUST cover: age "
            "18-55 years inclusive (adjust per "
            "population_config), BMI 18.50-29.99 kg/m^2, healthy male/female. Female subjects of "
            "childbearing potential must be non-pregnant, non-lactating, and using one of these "
            "medically acceptable contraception methods from consent through the post-dose window "
            "in the locked design parameters: abstinence; hormonal contraception used for at least "
            "one month before screening; a double-barrier method (e.g. diaphragm plus spermicide, "
            "or condom plus spermicide); an intrauterine device; or surgical sterility (e.g. "
            "bilateral tubal ligation or oophorectomy, or hysterectomy, with an absence of bleeding "
            "for at least three months) — OR be post-menopausal, defined as at least 45 years of "
            "age with amenorrhea for at least one year, or bilateral oophorectomy with or without "
            "hysterectomy. Male subjects with a partner of childbearing potential must use an "
            "effective, medically acceptable contraceptive method for the same window. Take the "
            "exact window duration from the locked design parameters where given; if none is given, "
            "state that it is to be confirmed rather than inventing a number. Inclusion MUST ALSO "
            "cover: normal/not-clinically-significant "
            "clinical laboratory values, negative HIV-1/2 + HBV + HCV serology, normal/NCS "
            "physical exam + vital signs + 12-lead ECG, and ability to provide "
            "informed consent. For a SMALL-MOLECULE study (study_type 'BE' or 'food_effect'), "
            "Inclusion MUST ALSO cover non-smoker status. For a biosimilar_pk/biologic study, do "
            "NOT include a non-smoker requirement or any other smoking-related eligibility "
            "criterion here — smoking status has no mechanism for a therapeutic protein and a "
            "`stratification` object or LOCKED STRATIFICATION block, when present, states this "
            "explicitly as a prohibition; follow it rather than defaulting to the small-molecule "
            "template. "
            "Exclusion MUST cover: inability to consent, alcohol/drug abuse or "
            "recent use, use of any investigational product within 30 days or 5 half-lives "
            "(whichever longer), recent prescription/OTC/herbal medication use, recent vaccination, "
            "significant acute or chronic illness, hypersensitivity to the drug class or "
            "formulation excipients, clinically significant abnormal vital signs at "
            "screening/check-in (give the specific BP/pulse/respiratory-rate/temperature bounds), "
            "difficult venipuncture history, recent blood donation, hospitalization or prolonged "
            "immobilization (e.g. >=14 days) within the past 3 months, any food allergy/"
            "intolerance/dietary restriction incompatible with the confinement diet, unavailability "
            "for the required confinement or scheduled visits, a planned surgical or outpatient "
            "procedure during the study, and a positive drug-of-abuse or alcohol screen at "
            "screening or check-in. "
            "WHEN a `stratification` object is present in context or a LOCKED STRATIFICATION "
            "block is supplied and it carries `suppressed_factors` (modality-excluded factors for "
            "a biologic), do NOT derive an inclusion or exclusion criterion from any of those "
            "excluded factors (in particular: no non-smoker requirement, no metabolizer- or "
            "transporter-genotype screening or exclusion, no proton-pump-inhibitor/H2-blocker/"
            "antacid restriction on absorption grounds, and no ancestry-based restriction on "
            "pharmacogenetic grounds) — these are ALREADY DETERMINED not to apply to this "
            "product. "
            "THEN, when a `ddi` object is present in context or a LOCKED PROHIBITED AND "
            "RESTRICTED CONCOMITANT MEDICATIONS block is supplied, the medication-related "
            "exclusion criteria MUST come from it and ONLY from it: add one exclusion "
            "criterion per PROHIBITED entry naming that entry's drug class/`label`, its "
            "`examples` and its EXACT `washout_days` before first dose, and one criterion "
            "covering every `history_exclusion_agents` entry (agents that cannot be washed out "
            "and are excluded on history alone, using the stated "
            "`screening_history_window_days`). Do NOT insert a generic 'CYP3A4 inhibitors or "
            "inducers' clause, or any other pathway clause, unless that pathway actually "
            "appears in the locked list — this drug's clearance pathways were determined from "
            "its own label. Do NOT invent, round or harmonise a washout period, and do NOT "
            "add drug classes that are not listed. Where the locked facts report `unresolved` "
            "items or `assumptions`, disclose them in one short sentence rather than hiding "
            "them. If no `ddi` object is present, write the medication exclusions generically "
            "as before; "
            "2) **Planned number of subjects** — state the total N, the randomisation ratio (1:1) "
            "and the per-arm N exactly as given in the locked design parameters; "
            "3) **Subject Withdrawal/Discontinuation and Replacement** — numbered list of "
            "withdrawal circumstances (consent withdrawal; AE/safety concern; protocol violation; "
            "non-compliance; prohibited-treatment use; lost to follow-up; death; other justifiable "
            "reason), documentation requirements, and state the replacement policy. WHEN a "
            "`randomization` object is present in context or a LOCKED RANDOMIZATION AND BLINDING "
            "block is supplied and its replacement-of-withdrawals fact states a rule, narrate "
            "THAT rule exactly — do not restate or paraphrase a different policy. OTHERWISE, "
            "default to: subjects withdrawn after randomization/dosing are NOT replaced. "
            "When a LOCKED SAFETY GATING RULES block is supplied, the safety-driven withdrawal "
            "circumstances in this list MUST be the engine's INDIVIDUAL SUBJECT STOPPING RULES, "
            "reproduced with their EXACT triggers and numeric thresholds, rather than a generic "
            "'adverse event' bullet — a rounded or softened threshold is not a stopping rule. "
            "Do NOT list the COHORT/STUDY HALTING rules here: those stop dosing of all further "
            "subjects and belong in Safety Monitoring; merging the two levels is the specific "
            "error this locked list exists to prevent. State that the cohort-level halting "
            "rules are specified in the Safety Monitoring section. Also state that a subject who "
            "fails screening may be rescreened once, at the investigator's discretion, after "
            "signing a new informed consent form and receiving a new screening identifier, and "
            "that whether a withdrawn or replaced subject is included in or excluded from the "
            "analysis sets is defined in the Statistical Analysis Plan or decided at the blinded "
            "data review meeting prior to database lock — do not decide that question here; "
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
        "context_keys": [
            "drug_inn", "pk_parameters", "dose_strength", "formulation", "label_data",
            "study_type", "stratification", "randomization", "ddi",
        ],
        "instructions": (
            "Where label_data is provided, align dose/route/formulation with the approved label "
            "and name the specific reference product (manufacturer and strength) rather than a "
            "placeholder - calling it the Reference Listed Drug (RLD) for a small-molecule "
            "bioequivalence study and the US-licensed reference product for a biosimilar "
            "(study_type = 'biosimilar_pk'), per the REGULATORY FRAMEWORK block. "
            "If dose_strength is NOT provided in context, DO NOT hedge with a "
            "parenthetical example (e.g. NEVER write 'to match Reference (e.g., 5 mg)') — commit to "
            "ONE specific, commonly-used approved strength for this drug (using label_data/general "
            "knowledge of its approved dosage forms) and state it plainly as the strength used, since "
            "a vague placeholder-like hedge is exactly the kind of unfilled-value a regulatory "
            "reviewer will flag. Write SEVEN subsections using <h2> headings, in this exact order: "
            "1) **Randomisation/Method of Assigning Subjects to Treatment Arms** — central "
            "randomisation (e.g. IRT/IWRS) in a 1:1 ratio, randomisation-code control and "
            "concealment from blinded staff. "
            "When a LOCKED RANDOMIZATION AND BLINDING block is supplied, take the method "
            "statement from it verbatim in substance: the exact design label, the EXACT "
            "sequence set (e.g. TRTR/RTRT for a 4-period full replicate — never a different "
            "sequence set), the allocation ratio, the permuted-block method and the stated "
            "balance guarantee. CRITICAL ALLOCATION-CONCEALMENT RULES, which override any "
            "instinct to be more informative: NEVER print the block size or any block-size "
            "range; NEVER print, list, tabulate or give an example of any individual subject's "
            "randomisation number, stratum assignment, sequence or treatment order; NEVER "
            "state the random seed or any means of regenerating the list. Instead state where "
            "the randomisation list is held (a restricted document outside the protocol body, "
            "released only to the unblinded personnel named in the locked facts) and reproduce "
            "the locked protocol-body statement about the block structure and balance. Also "
            "narrate the locked blinding posture and the replacement rule EXACTLY as given — "
            "in particular do NOT describe the study as double-blind if the locked facts "
            "specify open-label dosing with a blinded bioanalytical laboratory, and do NOT "
            "claim subject masking that the locked facts do not support. "
            "THEN, when a `stratification` object is present "
            "in context, add an <h3> sub-subsection 'Stratification Factors' that: states the "
            "randomisation is stratified on the listed factors; renders them as an HTML "
            "<table> with the columns 'Factor', 'Basis for PK Variability' and 'Rationale for "
            "Stratification' — one row per factor, in the order given, using that factor's "
            "`magnitude` for the middle column and its `rationale` plus `effect` for the last; "
            "and closes with one short paragraph drawn from `stratification.summary` "
            "explaining what stratification does and does NOT buy in THIS design (in a "
            "crossover each subject is their own control, so the benefit is balance across "
            "SEQUENCES and protection from sequence/period confounding, NOT a narrower "
            "confidence interval; in a parallel design it genuinely tightens the between-"
            "subject error term). The factor list, its order and the stated magnitudes are "
            "ALREADY DETERMINED — reproduce them faithfully and do NOT add factors, drop "
            "factors, re-rank them, or invent effect sizes. Where a factor carries a `note` "
            "about eligibility criteria already narrowing it, state that honestly rather than "
            "overclaiming. If no `stratification` object is present, omit this sub-subsection "
            "entirely; "
            "2) **Investigational Products** — Test and Reference product identity, strength, "
            "dosage form and manufacturer; "
            "3) **Management of Clinical Supplies** — THREE <h3> sub-subsections "
            "'Study Drug Packaging, Labeling, and Storage' (storage temperature/conditions, and "
            "that each immediate container's label carries: the protocol number; subject/study "
            "centre number; contents and quantity; randomisation code or kit number; investigator "
            "name; storage instructions; a caution statement, e.g. 'For clinical trial use only'; "
            "sponsor contact information; and the expiry date), "
            "'Study Drug Preparation and Administration' (dose, route, technique, and — for an "
            "ORAL small-molecule product only — the fasting requirement and post-dose fasting "
            "window; for a parenteral biologic/SC or IV product, describe the injection/infusion "
            "site, technique and post-dose observation instead, and do NOT impose a fasting "
            "requirement, since there is no oral absorption for fasting to standardise), "
            "'Study Drug Accountability, Dispensing, and Destruction'; "
            "4) **Treatment Compliance** — confirmation and documentation of full-dose "
            "administration; "
            "5) **Blinding** — TWO <h3> sub-subsections 'Blinding Procedures' (state the blinding "
            "level: double-blind is typical for a biosimilar PK similarity study, open-label is "
            "typical for a small-molecule BE crossover — choose based on study_type; when a "
            "LOCKED RANDOMIZATION AND BLINDING block is supplied it OVERRIDES this default and "
            "you must state the masking posture it specifies, including which parties are "
            "blinded and which are not) and "
            "'Breaking the Blind' (emergency unblinding procedure per the locked unblinding "
            "procedure where one is given, if double-blind; state N/A for an open-label design); "
            "6) **Prior, Concomitant, and Prohibited Medications** — recording window and "
            "prohibited medication classes consistent with the exclusion criteria. When a "
            "LOCKED PROHIBITED AND RESTRICTED CONCOMITANT MEDICATIONS block is supplied, render "
            "it as an HTML <table> with the columns 'Status', 'Class or Agent', 'Examples', "
            "'Washout Before First Dose' and 'Conditions/Rationale' — one row per PROHIBITED "
            "entry first, then each RESTRICTED entry, then each PERMITTED entry, in the order "
            "given, using each entry's own `washout_days` verbatim (state 'Not applicable' where "
            "no washout is given). These statuses, agents and washout periods are ALREADY "
            "DETERMINED from this drug's own label: do NOT add classes, do NOT change a status, "
            "do NOT invent or round a washout period, and do NOT add a generic CYP3A4 clause "
            "unless CYP3A4 is in the locked list. Every washout figure in this subsection must "
            "also appear in the Exclusion Criteria, and must not be confused with the washout "
            "BETWEEN TREATMENT PERIODS given in the locked design parameters — they are "
            "different quantities and must not be harmonised into one number. Where a "
            "post-dose restriction window is given, state it. If no such block is supplied, "
            "write this subsection generically as before; "
            "7) **Subject Restrictions During the Study** — for an ORAL small-molecule product, "
            "meal schedule and water restriction around dosing (standardising GI absorption); for "
            "a parenteral biologic, there is no food effect or oral absorption to standardise, so "
            "omit the meal-schedule/water-restriction language and instead state the standard meal "
            "schedule for safety/tolerability and any injection-site care instruction (e.g. avoid "
            "rubbing or applying pressure to the injection site); either way, cover alcohol/"
            "recreational-drug restriction and post-dose activity restriction."
        ),
    },
    "study_assessments": {
        "fda_ref": (
            "Reference template, Section 9 — Study Assessments (9.1 Pharmacokinetic Assessment, "
            "9.2 Immunogenicity Assessments, 9.3 Blood Sample Processing, 9.4 Safety and "
            "Tolerability Assessment); FDA Guidance on Bioanalytical Method Validation (May 2018)"
        ),
        "context_keys": [
            "pk_parameters", "study_type", "drug_inn", "analytical_method", "lloq",
            "sampling_schedule", "bioanalytical", "safety_gating", "comparative_safety"],
        "instructions": (
            "Write subsections using <h2> headings, in this exact order: "
            "1) **Pharmacokinetic Assessment** — "
            "WHEN a `sampling_schedule` object is present in context (equivalently, when a "
            "LOCKED PK BLOOD-SAMPLING SCHEDULE block is supplied): the schedule is ALREADY "
            "DETERMINED and you must REPRODUCE IT VERBATIM. Render `sampling_schedule."
            "timepoints` as a Markdown table with the EXACT headers '| Nominal Time (h) | "
            "Acceptable Window (min) | Volume per Draw (mL) | Cumulative Volume (mL) |', one "
            "row per entry in the given order, mapping the columns 1:1 onto that entry's "
            "`hours`, `window_minutes`, `volume_ml` and `cumulative_volume_ml`. Do NOT add, "
            "drop, merge, re-order, round or re-derive a single timepoint, window or volume, "
            "and do NOT substitute your own judgement about how many samples are needed. State "
            "the Tmax and terminal t1/2 the schedule was built on and their basis, the last "
            "nominal timepoint expressed in half-lives, the predicted %AUC extrapolated against "
            "its limit, the number of samples available for the terminal-slope (lambda_z) fit, "
            "and the pre-dose carry-over rule exactly as locked. State the total blood volume "
            "and the 8-week ceiling it is checked against, using the locked figures and the "
            "locked basis text — do not assert compliance with a limit the locked facts do not "
            "claim. Surface every locked assumption and every flagged item marked as requiring "
            "disclosure rather than omitting it. "
            "OTHERWISE (no sampling_schedule supplied), fall back to deriving the timing "
            "yourself from the drug's ACTUAL pharmacokinetics in pk_parameters (cite the "
            "specific t_half/Tmax values used): pre-dose sample within 60 minutes before "
            "dosing; at least 12-16 post-dose timepoints with 3-4 points around the literature "
            "Tmax and taper through the elimination phase so the last sample extends to "
            ">= 3-5 x terminal t1/2 (AUC0-t >= 80% of AUC0-inf); present the schedule as a "
            "Markdown table with the same EXACT headers using 4-6 mL per draw; state the "
            "sample collection time windows (e.g. +-2 min confinement, +-120 min ambulatory) "
            "and total blood volume across the whole study, confirming it stays within ~500 mL "
            "per any 8-week window; "
            "2) **Immunogenicity Assessments** — ONLY if study_type is 'biosimilar_pk': ADA/NAb "
            "sampling timepoints (use the locked immunogenicity timepoints where the sampling "
            "schedule supplies them; otherwise pre-dose plus follow-up days/weeks matching the "
            "PK tail, e.g. Day 29, 57, 85, and a Week-24 EOS sample), tiered testing strategy "
            "(screening ELISA/ECL bridging assay -> confirmatory competitive-inhibition assay "
            "-> NAb assay if positive), and sample handling. Omit this subsection entirely for "
            "non-biosimilar study types; "
            "3) **Blood Sample Processing / Bioanalytical Method** — central-laboratory analysis "
            "using a validated method. WHEN a `bioanalytical` object is present in context "
            "(equivalently, a LOCKED BIOANALYTICAL METHOD block): the assay class, calibration "
            "range, acceptance criteria, stability battery, sample-handling steps, "
            "chain-of-custody requirements, laboratory qualification requirements and the "
            "incurred-sample-reanalysis (ISR) plan are ALREADY DETERMINED — narrate them "
            "EXACTLY, and render the validation parameters, the stability battery and the "
            "sample-handling steps as HTML <table>s (parameter | requirement/acceptance "
            "criterion, one row per locked entry). Do NOT invent, round, rescale or recalculate "
            "any concentration, temperature, duration, percentage or acceptance criterion, and "
            "do NOT state a numeric LLOQ or ULOQ unless the locked facts supply one — where "
            "they report that the calibration range could not be derived, say so plainly rather "
            "than inventing a range. If the free-text `analytical_method` or `lloq` context "
            "hints disagree with the locked calibration range, treat the LOCKED value as "
            "authoritative and state the sponsor-specified value only as a stated override; "
            "never print two different LLOQs as if both applied. Name the bioanalytical "
            "facility only if the locked facts name one — otherwise state it is to be "
            "confirmed, and never invent a laboratory name. Disclose the locked assumptions and "
            "any flagged items. OTHERWISE (no bioanalytical object): for a small molecule "
            "describe LC-MS/MS (LLOQ/ULOQ, calibration range, accuracy/precision acceptance of "
            "+-15% RE / <=15% CV, ISR >= 10% of samples); for a biologic describe a "
            "ligand-binding assay (ELISA/ECL), minimum required dilution, and +-20% RE / "
            "<=20% CV acceptance; "
            "4) **Safety and Tolerability Assessment** — EIGHT <h3> sub-subsections: "
            "'Demographic Data' (DOB, height, weight, BMI, sex, race); "
            "'Vital Signs' (BP/pulse/respiration/temperature at check-in, pre-dose, and defined "
            "post-dose timepoints); "
            "'Injection Site Assessment' (ONLY for a biologic/SC product — pre- and post-dose "
            "local-tolerability grading; omit for an oral small molecule); "
            "'Electrocardiogram (ECG)' (12-lead at screening, check-in, EOS); "
            "'Physical Examination' (complete at screening/EOS, abbreviated at other visits); "
            "'Adverse Event Monitoring' (non-leading, open-ended querying at each safety contact); "
            "'Pregnancy testing' (for female subjects of childbearing potential only: a serum "
            "pregnancy test at screening and EOS, a urine pregnancy test before dosing, and that "
            "only a subject with a confirmed negative pre-dose result may receive study drug); "
            "'Clinical Laboratory Parameters' (state that hematology, serum chemistry and "
            "urinalysis are drawn at the timepoints in the Schedule of Assessments, analysed by "
            "the central laboratory, and reviewed by the Investigator; reference that the full "
            "parameter panel is listed in the Schedule of Assessments/laboratory table rather "
            "than repeating every analyte here); "
            "5) **Discharge Criteria** — include this subsection ONLY when a `safety_gating` "
            "object supplying `discharge_criteria` is present in context (equivalently, when "
            "the LOCKED SAFETY GATING RULES block contains discharge criteria); omit it "
            "entirely otherwise. Render the criteria as a numbered list in the given order, "
            "reproducing each criterion and its objective threshold EXACTLY — these are "
            "pre-specified boundaries, and an 'approximately' or rounded version of one is not "
            "a criterion. The criterion flagged as mandatory (documented authorisation by the "
            "Investigator or a qualified delegate) MUST appear explicitly and MUST NOT be "
            "softened. State the minimum post-dose observation period, what happens when the "
            "criteria are not met, and the readmission provision, all using the locked values. "
            "Where the locked facts carry a design note about a multi-period study (discharge "
            "from one period not authorising dosing in the next), state it. Do NOT restate the "
            "individual or cohort stopping rules here — they belong in Population and Safety "
            "Monitoring respectively. "
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
        "context_keys": ["drug_inn", "study_type", "safety_gating", "safety_reporting", "comparative_safety"],
        "instructions": (
            "Write ONE <h2> subsection '10.1 Handling and Reporting of Adverse Events' containing "
            "EIGHT <h3> sub-subsections in this exact order (plus the TWO additional "
            "sub-subsections 9 and 10 defined below when the locked safety gating rules are "
            "supplied): "
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
            "definition, and the distinction that 'severe' measures intensity, not seriousness) "
            "— when a LOCKED SAFETY GATING RULES block supplies a grading scale, render that "
            "scale's grades as an HTML <table> with the columns 'Grade', 'Title', 'Definition' "
            "and 'Protocol Consequence', one row per locked grade, taken VERBATIM, instead of "
            "describing the grades in prose, and state the locked severity-versus-seriousness "
            "and causality-basis wording; "
            "5) **Abnormal Lab Results** — criteria for reporting an abnormal result as an AE "
            "(discontinuation, intervention required, further diagnostic workup, clinically "
            "significant signs/symptoms); "
            "6) **Pregnancy Reporting** — exclusion/withdrawal of confirmed pregnancy, 24-hour "
            "sponsor notification, IRB notification within 72 hours, spontaneous abortion always "
            "treated as an SAE; "
            "7) **Reporting of Serious Adverse Events** — WHEN a LOCKED SAFETY REPORTING PLAN "
            "block is supplied it is the SINGLE source of every reporting deadline: state the "
            "regulatory regime and its basis, present the expedited-reporting test as a "
            "CONJUNCTION (all criteria must hold together — never as a list of alternatives), "
            "and render the reporting obligations as an HTML <table> with the columns 'Who "
            "Reports', 'What', 'To Whom', 'Deadline' and 'Regulatory Basis', one row per "
            "APPLICABLE locked duty, in the given order. Every day count, its unit and its "
            "clock start MUST be copied exactly from the locked plan — do NOT state a day "
            "count that does not appear there, do NOT convert calendar days into business or "
            "working days, and do NOT start a clock at anything other than the locked clock "
            "start. Where a duty is marked as an assumed convention rather than a figure fixed "
            "in regulation, say so plainly instead of attributing it to a guideline. Where the "
            "locked plan reports that the study is NOT under an IND, do not assert an FDA "
            "expedited-reporting obligation — state instead why those duties do not apply and "
            "that they take effect if an IND is opened. OTHERWISE (no locked plan supplied), "
            "use the default wording: 24-hour sponsor notification, written report within 72 "
            "hours, and expedited regulatory timelines (7 calendar days for fatal/life-"
            "threatening SUSARs, 15 calendar days for other SUSARs, per 21 CFR 312.32); "
            "8) **Concomitant Medication and Treatment** — recording window and the "
            "investigator/sponsor decision process for permitting concomitant medication during "
            "the study; "
            "9) **Individual Subject Stopping Rules** — include ONLY when a LOCKED SAFETY "
            "GATING RULES block is supplied; omit entirely otherwise. Reproduce the INDIVIDUAL "
            "SUBJECT STOPPING RULES as a numbered list in the given order, each with its EXACT "
            "trigger, numeric threshold and required action. Open with one sentence stating "
            "that these rules withdraw ONE subject from further dosing and never, by "
            "themselves, stop the study. Do NOT use the phrase 'halt dosing of all further "
            "subjects' or any equivalent in this sub-subsection; "
            "10) **Cohort Halting Rules and Resumption** — include ONLY when a LOCKED SAFETY "
            "GATING RULES block is supplied; omit entirely otherwise. Reproduce the "
            "COHORT/STUDY HALTING rules as a SEPARATE numbered list, never merged with the "
            "individual rules, each with its EXACT subject count, threshold and timeframe — a "
            "count rendered as 'two or more subjects' must not become 'several subjects', and "
            "a rounded count is not a rule. Then narrate the locked review-and-resumption "
            "requirements (who must review, what must be documented, what authorises "
            "resumption). Finally state the DISCLOSED ASSUMPTIONS from the locked block as "
            "pre-specified protocol conventions, explicitly NOT as regulatory requirements. "
            "Where the locked facts report a boxed warning detected on the product label, "
            "ensure it is acknowledged."
        ),
    },
    "data_handling": {
        "fda_ref": (
            "Reference template, Section 11 — Data Handling and Record Keeping (11.1 Source "
            "documents, 11.2 Data collection, 11.3 Database Management, 11.4 Record Retention); "
            "ICH E6(R2)"
        ),
        "context_keys": ["study_type", "org_name", "crf_dmp"],
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
            "submission) and that records are never destroyed without written sponsor approval. "
            "WHEN a `crf_dmp` object is present in context (equivalently, when a LOCKED CASE "
            "REPORT FORM / DATA MANAGEMENT block is supplied), expand the section as follows "
            "using ONLY the locked content — the forms, fields, permitted values, edit checks, "
            "standards versions, dictionaries, intervals and retention period are ALREADY "
            "DETERMINED and must not be invented, renamed, renumbered or rounded: "
            "(a) inside subsection 2 add an <h3> sub-subsection 'Case Report Form "
            "Specification' rendering the locked FORM INVENTORY as an HTML <table> with the "
            "columns 'Form', 'SDTM Domain', 'When Completed' and 'Repeating' — one row per "
            "locked form in the given order — followed by, for the forms marked CRITICAL only, "
            "a per-form HTML <table> of 'Field', 'Type', 'Units', 'Permitted Values' and "
            "'Required'; do not expand the non-critical forms field-by-field; "
            "(b) also inside subsection 2, state the actual-versus-nominal PK draw time rule "
            "exactly as locked (actual clock times are recorded and used for the "
            "pharmacokinetic analysis; nominal times are used only for presentation), and the "
            "locked time-recording precision; "
            "(c) inside subsection 3 narrate the locked Data Management Plan sections in the "
            "order given — data flow, database build and user-acceptance testing, external data "
            "transfers, medical coding, query management, SAE reconciliation, protocol-deviation "
            "and blinded data review, database lock, Part 11 audit trail, data privacy and "
            "archival — using the locked query-response target, SAE reconciliation interval and "
            "coding dictionaries (state each dictionary and its version policy verbatim), and "
            "render the edit checks as a summary <table> of 'Check Type', 'Severity' and "
            "'Count' from the locked counts plus a handful of the locked example rules stated "
            "verbatim; do NOT invent an edit check that is not listed; "
            "(d) state the locked data standards (including the SDTM implementation-guide "
            "version) and note explicitly where the locked facts say a version must be "
            "re-confirmed against the current regulatory data-standards catalogue at "
            "submission; "
            "(e) use the locked retention period rather than the default wording above if the "
            "two differ, and disclose the locked assumptions as pre-specified conventions "
            "rather than regulatory requirements; "
            "(f) surface every locked flag marked as requiring disclosure in the protocol. "
            "This is a protocol-package specification of what will be captured and how the "
            "data will be managed — do NOT write it as if the database already exists, and do "
            "NOT invent site names, vendor names or system names."
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
        "context_keys": [
            "pk_parameters", "study_type", "acceptance_criteria", "cv_intra",
            "sample_size", "meta_analysis", "regulatory_sources", "randomization", "multiplicity"],
        "instructions": (
            "Write the complete statistical analysis plan, tailored to study_type, as ELEVEN <h2> "
            "subsections in this exact order, FOLLOWED BY the meta-analysis appendix described "
            "at the end of these instructions: "
            "1) **Study Populations** — define the Randomised Set, Pharmacokinetic Set (dosed with "
            "reliable bioanalytical data; BLQ concentrations treated as zero), and Safety Set "
            "(received any dose, analysed as treated); briefly note disposition-of-subjects "
            "reporting will summarise enrolment/completion by treatment arm; state that protocol "
            "deviations will be classified as minor or major before unblinding, and that "
            "deviations affecting eligibility or other study procedures will be summarised by "
            "treatment arm; "
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
            "Per-Protocol/PK Set; (e) state the replacement policy for subjects withdrawn or "
            "discontinued after dosing. WHEN a `randomization` object is present in context or a "
            "LOCKED RANDOMIZATION AND BLINDING block is supplied and its replacement-of-"
            "withdrawals fact states a rule, narrate THAT rule exactly and do not additionally "
            "state they are not replaced; OTHERWISE default to: subjects withdrawn or "
            "discontinued after dosing are NOT replaced; sensitivity analyses with and without "
            "excluded/replaced subjects will be reported; "
            "4) **Immunogenicity Analyses** — ONLY if study_type is 'biosimilar_pk': descriptive "
            "ADA/NAb incidence, titre, and an exploratory comparison of PK between ADA-positive and "
            "ADA-negative subjects. Omit entirely for non-biosimilar study types; "
            "5) **Safety Analyses** — one lead-in sentence that analyses use the Safety Set, then "
            "labelled <h3> sub-subsections, each a short paragraph: 'Adverse Events' (descriptive "
            "summary and individual listings of AEs/SAEs, MedDRA-coded by System Organ Class and "
            "Preferred Term); 'Laboratory Tests' (summary and listing of hematology/chemistry/"
            "urinalysis results, flagging clinically significant abnormalities); 'Vital Signs' "
            "(summary and listing, flagging clinically significant abnormalities); "
            "'Electrocardiogram (ECG)' (summary and listing, flagging clinically significant "
            "findings); 'Physical Examination' (summary of findings); 'Prior and Concomitant "
            "Medications' (summarised and listed by treatment arm); and, ONLY for a biologic/"
            "parenteral product, 'Injection Site Reaction' (summarised and listed in the same "
            "manner as adverse events) — omit this last sub-subsection entirely for an oral "
            "small molecule; "
            "6) **Sample Size Determination** — FIRST read the CV PROVENANCE line in the locked "
            "design parameters; it decides how this subsection justifies the CV, and it "
            "overrides the default wording below. THROUGHOUT this subsection, state the CV used "
            "to power the study using the correct term for the locked design: 'within-subject "
            "CV' for a crossover or replicate design, or 'between-subject CV' for a parallel "
            "design (a parallel design has no within-subject comparison) — read the design from "
            "the locked design parameters and do not default to 'within-subject' "
            "unconditionally. "
            "(a) DEFAULT / LITERATURE-DERIVED CV (the CV PROVENANCE line says the CV is "
            "literature-derived, or no such line is present): the CV used to "
            "power the study is the "
            "HIGHEST CV observed for either Cmax or AUC across published "
            "single-dose PK studies IN THE SAME POPULATION THIS PROTOCOL ENROLS — read the "
            "protocol population from the META-ANALYSIS locked block and name it explicitly "
            "(healthy volunteers, or the patient population); never assume healthy volunteers. "
            "Studies in a different population were retrieved and are tabulated in the "
            "meta-analysis appendix, but they are NOT the basis of the sample size; if the "
            "locked block reports a higher CV in a non-matching population, state in one "
            "sentence that it was reviewed and deliberately not adopted, and why. Taken as a "
            "conservative basis. If the locked design parameters include a 'cv_table' (a list of "
            "trials with their source, population, parameter, and CV%), render it as an HTML "
            "<table> with columns Study/Source | Population | Parameter | Within-subject CV% — "
            "keep this EXACT column header regardless of THIS protocol's own design, because "
            "every literature value in this table is a within-subject/intra-subject CV from a "
            "crossover or replicate study. Some trials in this table give CV% as a derived value "
            "(the trial reported a mean and a standard deviation rather than a percentage; the app "
            "computed CV% = SD/mean x 100 from those two numbers) rather than a percentage the "
            "trial stated outright — a `cv_metrics` entry marked derived_from_mean_sd carries this. "
            "Where that applies to the row you are rendering, append '(derived from mean/SD)' after "
            "the percentage in that cell rather than presenting it as a value the trial printed — "
            "and add a "
            "final row identifying the maximum CV adopted; explicitly state which study and which "
            "parameter (Cmax vs AUC) gave the highest CV. When the locked design is PARALLEL, add "
            "one further sentence directly beneath the table stating plainly that these "
            "tabulated literature values are within-subject CVs from crossover/replicate "
            "studies — informative evidence, but not on the same basis as, and not directly "
            "comparable to, this study's own between-subject CV assumption. "
            "(b) INVESTIGATOR-SPECIFIED CV (the CV PROVENANCE line says the CV is "
            "investigator-specified / a sponsor override): state plainly, in one sentence, that "
            "the CV used for powering (using the correct within-/between-subject term for the "
            "locked design) was specified by the investigator/sponsor "
            "rather than derived from the published literature, give the value, and state that "
            "its justification is the sponsor's own (for example internal or pilot data) and is "
            "held outside this protocol. In this case you MUST NOT render the 'cv_table' at "
            "all, MUST NOT describe the literature trials as the basis, source, support or "
            "provenance of the adopted CV, and MUST NOT write that the CV was 'observed', "
            "'pooled', 'taken from' or 'confirmed by' published studies. If a cv_table is "
            "nevertheless present in context you may mention the literature values only as "
            "separately-reported context, clearly labelled as NOT the source of the value used "
            "— never as its justification. "
            "(c) ASSUMED CV (the CV is marked 'assumed' because no value was available): say so "
            "and note that values from FDA/EMA reviews, AUSPAR, and Japanese (PMDA) or Chinese "
            "(NMPA) registration studies should be sought to confirm it; do not present a "
            "cv_table as its provenance. WHEN a `regulatory_sources` object or LOCKED FACTS - "
            "REGULATORY ASSESSMENT-REPORT EVIDENCE block is supplied, follow its SOURCE RULE "
            "exactly: if it lists a referenced source, name it and attribute the statement to the "
            "PMID of the retrieved paper that reports it, never as though this protocol consulted "
            "the assessment report directly; if it reports NO referenced source, say plainly that "
            "no such regulatory-report evidence was retrieved rather than inventing one. "
            "In every case: state the assumed GMR, target power, and "
            "significance level EXACTLY as given in the locked design parameters, the planned "
            "enrolment (with dropout allowance) and the resulting evaluable N. State simply that the "
            "sample size was calculated in R using the two one-sided tests (TOST) method — do NOT "
            "reproduce the calculation, formulas, or console output. Do NOT recompute or "
            "restate a different sample size — cite the planned N exactly as given; "
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
            "changes to primary endpoints or acceptance criteria are permitted. "
            "FINALLY, when a `meta_analysis` object is present in context (equivalently, a "
            "META-ANALYSIS OF PUBLISHED PK STUDIES locked block), append an <h2> subsection "
            "titled 'Appendix: Meta-Analysis of Published Pharmacokinetic Studies'. Render EVERY "
            "row of the locked table as an HTML <table> with columns "
            "Study | Indication | AUC | Cmax | Within-subject CV%, in the locked order — keep "
            "this EXACT column header regardless of THIS protocol's own design, because every "
            "CV in this appendix is a within-subject/intra-subject literature value. "
            "Reproduce the values EXACTLY as supplied — do NOT recompute, round, convert units, "
            "reorder, add studies, or drop studies. Where the locked table says 'not reported', "
            "write 'Not reported' in that cell; NEVER fill an empty cell from your own knowledge, "
            "because an invented PK value in a meta-analysis is a fabrication rather than a gap. "
            "Group or clearly mark the studies that are NOT in the protocol's population and "
            "state, in one sentence directly beneath the table, that they were reviewed but "
            "excluded from the sample-size determination because they do not describe the "
            "population this protocol enrols. Close with one sentence naming the adopted CV, the "
            "study it came from, and the fact that it is the highest among population-matched "
            "studies. When the locked design is PARALLEL, add one further sentence stating "
            "plainly that every CV in this appendix table is a within-subject value from "
            "crossover or replicate literature studies, offered as informative context only — "
            "not on the same basis as, and not directly comparable to, this protocol's own "
            "between-subject CV assumption. State the search basis honestly: these are the "
            "studies retrieved by the literature search, not a claim of exhaustive systematic "
            "review."
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
        # NOTE: "icf" is deliberately absent. The consent FORM's document
        # control (version, regulatory basis, element coverage, pending
        # site-specific fields) belongs to the form, which is a separate
        # deliverable on its own approval cycle — see _ENGINE_DIRECTIVE_PLAN.
        "context_keys": ["org_name", "irb_info", "study_type", "safety_reporting"],
        "instructions": (
            "Write FOUR subsections using <h2> headings, in this exact order: "
            "1) **Ethical principles, laws, and regulations** — conduct per the Declaration of "
            "Helsinki, ICH E6(R2) GCP, and applicable local regulatory requirements (21 CFR "
            "Parts 50/56 for US sites); "
            "2) **Ethics Committee/Institutional Review Board** — protocol and consent-material "
            "submission and written approval required before enrolment; amendments submitted "
            "before implementation (except to eliminate immediate hazard); annual progress "
            "reporting. When a LOCKED SAFETY REPORTING PLAN block is supplied, state the IRB "
            "notification duties it contains (what must be reported to the IRB, by whom, and "
            "within what period) using its EXACT deadlines and clock starts, and where a "
            "deadline is marked as an assumed convention rather than a figure fixed in "
            "regulation, say so and note that a stricter IRB standard operating procedure "
            "governs where one applies. Do NOT invent an IRB reporting deadline that the locked "
            "plan does not state. Where the locked plan states that the study is or is not an "
            "applicable clinical trial for public registry purposes, state that conclusion and "
            "its basis rather than asserting a registration obligation the plan does not "
            "support; "
            "3) **Informed Consent Process** — describe only the PROCESS by which consent is "
            "obtained: written consent per the 21 CFR 50.25 required elements, obtained before "
            "any study procedure (including screening), in language the subject understands, by "
            "a qualified delegated team member, with adequate time to read and ask questions, a "
            "copy provided to the subject, and the subject free to withdraw at any time without "
            "prejudice. Note that consent is documented per 21 CFR 50.27 and that a procedure "
            "performed before consent is signed is a protocol deviation. "
            "Say NOTHING about the Informed Consent Form as a document. The consent form is a "
            "separate deliverable on its own approval cycle, and its document control belongs "
            "to it, not to this protocol: do NOT state or cite its title, its version number or "
            "date, the regulatory basis it is written against, which required elements it does "
            "or does not cover, or any missing element or pending site-specific field "
            "(investigator/site identification, contacts, compensation, IRB details). Do NOT "
            "reproduce the form, its sections, its plain-language text or its signature block. "
            "A single clause noting that the consent form is submitted to the IRB/IEC together "
            "with this protocol is permitted, and nothing further about the form itself; "
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
        self,
        literature: list[dict],
        drug_inn: str,
        label_text: str | None = None,
        regulatory_plan: dict[str, Any] | None = None,
    ) -> list[dict]:
        """Extract PK parameters from literature abstracts and the FDA label.

        Uses tool_choice forcing so Claude always calls record_pk_parameters.
        Every returned parameter is guaranteed to have a source_pmid that matches
        a PMID from the input literature list, or "DAILYMED" for values taken from
        the FDA-approved label — which reliably reports terminal half-life, Tmax
        and Cmax that PubMed abstracts often omit. This materially raises the yield
        of t_half / Tmax / CV, which drive washout, sampling and sample size.

        ``regulatory_plan`` (the ``plan_regulatory_sources()`` result, when the
        caller ran the regulatory-targeted literature query) is rendered via
        ``regulatory_extraction_hint`` and appended to the prompt when it has
        anything to say, so values an abstract attributes to a regulatory
        assessment report are prioritised for extraction. Returns "" (no hint
        appended) when no source was referenced, so this is a no-op for every
        existing caller that does not pass it.

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
                    "content": _build_extraction_prompt(
                        docs, drug_inn, regulatory_extraction_hint(regulatory_plan)
                    ),
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


def _build_extraction_prompt(
    literature: list[dict], drug_inn: str, regulatory_hint: str = ""
) -> str:
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
        "and put 'h' in `unit`.\n"
        "8. A source reporting 'mean (+/- SD)' for Cmax/AUC0t/AUC0inf WITHOUT a separate "
        "percentage CV is JUST AS USABLE as one that states a CV% directly — this is the "
        "normal reporting style for a biologic/monoclonal-antibody label or a "
        "first-in-human paper, not a gap to skip. Record the mean as you normally would "
        "AND put the paired standard deviation in that same row's `sd` field. Do not "
        "calculate a percentage yourself and do not omit the entry because no CV% is "
        "printed — the app derives CV deterministically from mean and sd afterward. This "
        "applies to every source, not only DAILYMED: journal abstracts commonly report "
        "mean +/- SD too.\n\n"
        + (f"{regulatory_hint}\n\n" if regulatory_hint else "")
        + f"Literature:\n\n{joined}\n\n"
        "Call record_pk_parameters with all parameters you can identify."
    )


def _build_section_prompt(
    section_name: str, config: dict[str, Any], context: dict
) -> tuple[str, str]:
    section_title = section_name.replace("_", " ").title()
    # Engine results are projected BEFORE serialisation: the raw dicts are far
    # too large for a prompt, and their authoritative form is the directive.
    context_for_json = _project_engine_context(context)
    context_block = (
        json.dumps(context_for_json, indent=2, default=str) if context_for_json else "{}"
    )

    study_type = context.get("study_type", "BE")
    guideline_version = context.get("guideline_version")
    system_prompt, protocol_type = _expertise_for(study_type, guideline_version)

    design_directive = _locked_design_directive(context.get("sample_size"))
    reg_directive = regulatory_directive(guideline_version or "", study_type)
    decisions_directive = _design_decisions_directive(context.get("design_decisions") or [])
    # Deterministic-engine locked facts, rendered by the engines that own the
    # numbers. Sits immediately after the locked design block so every "ALREADY
    # DETERMINED" fact in the prompt is in one contiguous region.
    engine_directive = _engine_directives(section_name, context)

    user_prompt = (
        f"Generate the '{section_title}' section for a {protocol_type} study protocol.\n\n"
        f"Regulatory Reference:\n{config['fda_ref']}\n"
        f"{reg_directive}\n"
        f"{decisions_directive}\n"
        f"Instructions:\n{config['instructions']}\n"
        f"{design_directive}\n"
        f"{engine_directive}\n"
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
    hardcoded FDA voice. The biosimilar path defaults to FDA 351(k) - this app's
    biosimilar immunogenicity pathway is modeled on FDA guidance, and a blank or
    unknown guideline resolves to the FDA profile - but a biosimilar under a
    NON-FDA guideline gets that regulator's own framing: 351(k)/BPCIA is a US
    statute, and asserting it over an EMA protocol would also contradict the
    regulatory-framework directive (services/guidelines.py).
    """
    if study_type == "biosimilar_pk":
        prof = get_profile(guideline_version or "")
        if prof.regulator == "FDA":
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
        else:
            expertise = (
                f"You are a senior clinical pharmacologist and regulatory writer specializing in "
                f"biosimilar development for the {prof.regulator} ({prof.region}), working to "
                f"{governing_citation(guideline_version or '', study_type)}, ICH E6(R2) GCP, and "
                f"the applicable electronic-records requirements. You have deep expertise in "
                f"immunogenicity assessment, ligand binding assays, and PK similarity evaluation "
                f"for biological products. You produce audit-ready, regulatory-submission-quality "
                f"text. Refer to the comparator as the "
                f"{reference_product_term(guideline_version or '', study_type)}. "
                f"Write in formal third-person scientific language suitable for {prof.regulator} "
                f"biosimilar review. Output only the section body — no headings, numbering, or "
                f"meta-commentary."
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


_CV_OVERRIDE_MARKERS = ("override", "investigator", "sponsor", "user", "manual")


def cv_is_user_override(ss: dict[str, Any] | None) -> bool:
    """True when the powering CV was specified by the investigator/sponsor.

    Deterministic-fact rule: the model must never be left to infer provenance by
    reading a free-text ``cv_source`` string. The verdict is decided HERE, in
    Python, and injected as a locked fact, so the SAP and synopsis cannot present
    the literature ``cv_table`` as the justification for a number that did not
    come from the literature.

    Accepts either an explicit ``cv_is_user_override`` boolean (preferred, set by
    the override path) or a ``cv_source`` string carrying one of the override
    markers -- whichever the sample-size engine happens to emit.
    """
    if not ss:
        return False
    flag = ss.get("cv_is_user_override")
    if flag is not None:
        return bool(flag)
    src = str(ss.get("cv_source") or "").lower()
    return any(marker in src for marker in _CV_OVERRIDE_MARKERS)


def _cv_basis_label(design_key: str | None) -> str:
    """The correct name for the CV that powers this study, given its design.

    A crossover or replicate design genuinely compares each subject against
    themselves, so the powering CV is within-subject (intra-subject). A
    PARALLEL design has no within-subject comparison at all -- the CV that
    powers it is a between-subject (inter-subject) quantity, and calling it
    "within-subject" is a real, regulator-visible contradiction (confirmed in
    a live generated document). Any unrecognised/missing design_key falls
    back to "within-subject" to preserve behavior for callers that predate
    this helper.
    """
    if design_key == "parallel":
        return "between-subject"
    return "within-subject"


def _cv_provenance_lines(ss: dict[str, Any]) -> list[str]:
    """The CV PROVENANCE locked-fact line, or nothing.

    Only emitted for the investigator-specified case: the literature-sourced
    default wording is deliberately left exactly as it was, so a non-override
    protocol reads identically to before this engine wiring landed.
    """
    if not cv_is_user_override(ss):
        return []
    basis = _cv_basis_label(ss.get("design_key"))
    return [
        f"- CV PROVENANCE: INVESTIGATOR-SPECIFIED. The {basis} CV above was "
        "supplied by the investigator/sponsor; it was NOT derived from the "
        "published literature. State that plainly wherever the CV is quoted "
        "(Statistical Analysis Plan, synopsis). Do NOT render the literature "
        "'cv_table' as its justification, source or provenance, and do NOT write "
        "that the value was observed, pooled, taken from, or confirmed by "
        "published studies.",
    ]


def _power_line(ss: dict[str, Any]) -> str:
    """The "Power: NN%" fragment of the locked design block.

    ``ss['power']`` is the PER-ENDPOINT value the sample size was actually
    computed at (services/biostatistics.py) — for a multiple-dose biosimilar
    with two co-primary endpoints this is raised ABOVE the requested overall
    power (e.g. 94.87% to hold 90% overall). Printing that raw value here as
    "Power: 95%" would state a design power the study was never asked to have,
    in the block injected into the synopsis, objectives, study design and
    population sections — none of which also receive multiplicity_directive()
    (only statistical_analysis_plan and endpoints do). This headlines the
    OVERALL power (what the study is actually required to deliver) and, only
    when there is more than one co-primary endpoint, discloses the raised
    per-endpoint figure alongside it so the two numbers are never presented as
    contradicting each other.
    """
    overall = ss.get("overall_power")
    per_endpoint = ss.get("power")
    # power_adjusted, not "more than one co-primary endpoint", is the correct
    # gate: even the CONVENTIONAL Cmax + AUC(0-inf) pair counts as two
    # endpoints, so a length check alone would wrongly headline that pair's
    # independence-based lower bound (~81%) as if it were the design's real
    # target power.
    if overall is None or not ss.get("power_adjusted", True):
        return f"Power: {per_endpoint * 100:.0f}%;"
    return (
        f"Power: {overall * 100:.0f}% (overall — the probability every co-primary "
        f"endpoint passes; each endpoint is individually powered at "
        f"{per_endpoint * 100:.2f}% to achieve this, see the multiplicity note below);"
    )


def _endpoint_override_lines(ss: dict[str, Any]) -> list[str]:
    """Extra locked-facts lines that OVERRIDE every section's own static
    single-dose instructions when the co-primary set is NOT the conventional
    single-dose Cmax + AUC(0-inf) pair.

    THE PROBLEM THIS FIXES: _locked_design_directive() (this function's
    caller) is injected into EVERY section whose context includes
    sample_size — synopsis, study design, objectives, subject selection AND
    the SAP (see its own docstring) — and several of those sections'
    _SECTION_CONFIGS instructions are large STATIC strings that unconditionally
    describe a single-dose NCA recipe: the SAP names "Cmax and AUC0-inf",
    derived via a terminal log-linear regression (kel), with AUC0-inf =
    AUC0-t + Clast/kel; the endpoints section additionally lists AUC0-t,
    Vz/F, CL/F, kel and t1/2 as SECONDARY endpoints (Vz/F and CL/F are
    themselves derived from kel/AUC0-inf, so they inherit the same problem).
    For a multiple-dose biosimilar study the co-primary endpoints are
    actually first-dose and steady-state AUC(0-tau)
    (services/biostatistics.py's co_primary_endpoints_for) — a genuinely
    DIFFERENT set the "Co-primary endpoints" line just above already states
    correctly, so the model is handed contradictory statements of what it is
    even measuring in EVERY section this directive reaches. Nothing about
    AUC(0-inf)/kel/t1/2/Vz/F/CL/F is derivable from either profile, since
    each one covers only ONE dosing interval (services/sampling_schedule.py's
    own lambda_z_required=False for exactly this design already says so).
    The override text below is deliberately generic about WHERE the
    contradicting instructions live (rather than naming "the SAP section"),
    since this same function is injected into sections whose own static text
    differs.
    """
    co_primary = ss.get("co_primary_endpoints") or []
    if len(co_primary) <= 1 or not any("tau" in e.lower() for e in co_primary):
        return []
    return [
        "- ENDPOINT OVERRIDE: this document's standard single-dose analysis language "
        "(wherever it appears in this section's own instructions — Cmax + AUC(0-inf), "
        "kel-based terminal slope, AUC0-inf = AUC0-t + Clast/kel, or a secondary-endpoint "
        "list naming kel, t1/2, Vz/F or CL/F) does NOT apply to this study. Use ONLY the "
        "co-primary endpoints stated above, plus their own listed secondary endpoints "
        "(Ctrough, Tmax, accumulation ratio). Do NOT derive, report, or reference "
        "AUC(0-inf), kel, t1/2, Vz/F, or CL/F for either profile — each covers exactly "
        "one dosing interval, so a terminal elimination slope cannot be fitted from it "
        "and nothing computed from that slope is estimable either.",
    ]


def _regimen_override_lines(ss: dict[str, Any]) -> list[str]:
    """Locked-facts line for the patient multiple-dose biosimilar design.

    Several sections' static instructions unconditionally describe healthy
    subjects, "no direct therapeutic benefit", and a single-dose study. Those
    contradict a study whose co-primary set is first-dose AND steady-state
    AUC(0-tau) (services/biostatistics.py co_primary_endpoints_for), which
    only exists for patients on a repeated therapeutic regimen. Detection is
    keyed on that exact pair, so it is deterministic and never fires for
    single-dose or healthy-volunteer designs.
    """
    co_primary = [str(e).strip() for e in (ss.get("co_primary_endpoints") or [])]
    if not (
        any(e.startswith("First-dose AUC(0-tau)") for e in co_primary)
        and any(e.startswith("Steady-state AUC(0-tau)") for e in co_primary)
    ):
        return []
    return [
        "- POPULATION / REGIMEN OVERRIDE: this study enrols PATIENTS who receive the "
        "labelled therapeutic regimen REPEATEDLY until steady state. Wherever this "
        "section's own instructions describe healthy subjects or healthy volunteers, a "
        "single dose or single-dose design, 'no direct therapeutic benefit', or a "
        "single-dose confinement, replace that with the patient multiple-dose "
        "equivalent: patients may derive therapeutic benefit from treatment, and PK is "
        "evaluated after the first dose and at steady state. Never describe this study "
        "as single-dose or as enrolling healthy volunteers.",
    ]


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
        *_endpoint_override_lines(ss),
        *_regimen_override_lines(ss),
        f"- Assumed {_cv_basis_label(ss.get('design_key'))} CV: {ss['cv_pct']}% "
        f"(source: {ss['cv_source']})",
        *_cv_provenance_lines(ss),
        f"- True GMR assumption: {ss['gmr']:.2f}; {_power_line(ss)} alpha: {ss['alpha']}",
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
            f"figure everywhere the washout BETWEEN TREATMENT PERIODS is mentioned "
            f"(synopsis, study design); never state a generic '7 days' unless it "
            f"happens to equal this value. This figure applies ONLY to the interval "
            f"between dosing periods. It is NOT the washout of a prohibited "
            f"concomitant medication before first dose — those are separate, "
            f"per-agent periods given in the locked concomitant-medication facts, "
            f"and the two must never be merged, harmonised, or substituted for one "
            f"another."
        )
    if ss.get("needs_simulation"):
        lines.append(
            "- NOTE: this scaled/replicate method's final N must be confirmed by "
            "simulation; present the stated N as the planned/conservative estimate."
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Deterministic-engine locked facts
#
# Every engine result reaches the prompt by TWO complementary routes:
#   1. its *_directive() text, assembled below and injected next to the locked
#      washout/design block. This is the authoritative form: it is written by the
#      engine that owns the numbers, opens with an "ALREADY DETERMINED" header,
#      and tells the model exactly what it may not re-derive.
#   2. a PROJECTION of its result dict inside the JSON context block, carrying the
#      row-level fields a table needs (and only those).
#
# The projection exists because the raw engine dicts are not prompt-sized: the CRF
# specification alone serialises to ~336 KB, the ICF to ~69 KB. Dumping them whole
# would multiply the cost and latency of every generation while duplicating facts
# the directive already renders better. The whitelists below are therefore a
# deliberate prompt-hygiene step, NOT a filter on what the model is allowed to
# know -- anything dropped here is either present in the directive or is not
# protocol-body content.
# ---------------------------------------------------------------------------

# section name -> ((context key, renderer), ...). A renderer takes the engine's
# result dict and returns prompt text, or "" when the engine produced nothing.
_ENGINE_DIRECTIVE_PLAN: dict[str, tuple[tuple[str, Any], ...]] = {
    "synopsis": (
        ("sampling_schedule", sampling_schedule_directive),
    ),
    "population": (
        # The selected indication AND the naive/con-med eligibility criteria it
        # implies are written HERE — this is the section that states who is
        # enrolled, so the indication choice and the criteria that deliver it
        # must not be split across sections.
        ("patient_population", patient_population_directive),
        # Real prior-trial precedent for the Inclusion/Exclusion Criteria lists
        # — placed right after patient_population since it grounds criteria
        # for the SAME selected indication.
        ("trial_precedent", trial_precedent_directive),
        ("ddi", ddi_directive),
        ("safety_gating", safety_gating_directive),
        # Eligibility criteria are written HERE, so the population section is
        # exactly where a model would otherwise invent a non-smoker requirement
        # or a genotype exclusion from small-molecule habit — the modality
        # prohibition in stratification_directive() has to reach this section,
        # not just "treatment" (see project_biologic_protocol_review finding).
        ("stratification", stratification_directive),
        # Subsection 6.3 "Subject Withdrawal/Discontinuation and Replacement" is
        # written HERE. Without this, "population" would fall back to a
        # hardcoded "not replaced" default even when the randomization engine's
        # own replacement policy (rendered in "treatment") says otherwise — the
        # exact contradiction found in a real generated document.
        ("randomization", randomization_directive),
    ),
    "treatment": (
        ("randomization", randomization_directive),
        ("ddi", ddi_directive),
        # "treatment" already renders the raw `stratification` JSON into a
        # table per its own instructions; the directive is the authoritative,
        # narrate-don't-invent form of the SAME facts (and, for a biologic,
        # carries the modality-exclusion prohibition too).
        ("stratification", stratification_directive),
    ),
    "study_assessments": (
        ("sampling_schedule", sampling_schedule_directive),
        ("bioanalytical", bioanalytical_directive),
        ("safety_gating", safety_gating_directive),
        # Dose count to steady state and the ADA sampling months — the
        # assessment schedule has to reflect both.
        ("comparative_safety", comparative_safety_directive),
    ),
    "safety_monitoring": (
        ("safety_gating", safety_gating_directive),
        ("safety_reporting", safety_reporting_directive),
        # The head-to-head comparative safety/immunogenicity period, and the
        # fact that it continues well past the primary PK readout.
        ("comparative_safety", comparative_safety_directive),
    ),
    "data_handling": (
        # scope="all": the DMP narrative feeds subsections 1/3/4 and the CRF
        # specification feeds subsection 2 (eCRF / data collection).
        ("crf_dmp", lambda r: crf_dmp_directive(r, "all")),
    ),
    "statistical_analysis_plan": (
        # Co-primary alpha and power: which alpha each endpoint is tested at,
        # whether it was adjusted and why, and the per-endpoint power needed to
        # hold OVERALL power at target. Placed FIRST so the SAP's own sample-size
        # narrative is written against it rather than restating a convention.
        ("multiplicity", multiplicity_directive),
        # The per-study meta-analysis table AND the CV the study is powered on
        # come from one engine call, so the appendix and the sample-size
        # justification cannot state different numbers.
        ("meta_analysis", meta_analysis_directive),
        # This section already narrates CV%/PK-evidence provenance (the CV
        # PROVENANCE line); the regulatory-assessment-report evidence is the
        # same kind of provenance fact, so it belongs next to it rather than
        # in a section with no sourcing discussion at all.
        ("regulatory_sources", regulatory_sources_directive),
        # Subsection 3(e) states the withdrawal-replacement policy. Without
        # this, the SAP would fall back to a hardcoded "not replaced" default
        # even when the randomization engine's own replacement policy says
        # otherwise — the exact contradiction found in a real generated
        # document (SAP said "not replaced" while treatment said "may be
        # replaced" for the same protocol).
        ("randomization", randomization_directive),
    ),
    "study_rationale": (
        # The dose/PK rationale is exactly where a regulatory-assessment-report
        # citation (or the honest absence of one) belongs — this section
        # already justifies the dose from label_data/pk_parameters.
        ("regulatory_sources", regulatory_sources_directive),
    ),
    "ethics": (
        ("safety_reporting", safety_reporting_directive),
        # NO ("icf", icf_directive) HERE, DELIBERATELY. The consent form is a
        # separate deliverable (services/icf.py, exported via /export/icf/docx)
        # on its own approval cycle, and its document control — version, date,
        # regulatory basis, which 21 CFR 50.25 elements it covers, which
        # site-specific fields are still pending — belongs to that document.
        # Injecting the directive here made the protocol assert all of it, so
        # the protocol carried a second, uncontrolled copy of the form's
        # identity that goes stale the moment the form is revised. The protocol
        # now describes only the consent PROCESS (rendered as an appendix; see
        # routers/export.py::_split_informed_consent). icf_directive is still
        # used to build the standalone consent document itself.
    ),
}

# Every engine context key this module knows about, in a stable order.
_ENGINE_CONTEXT_KEYS: tuple[str, ...] = (
    "sampling_schedule",
    "ddi",
    "safety_gating",
    "bioanalytical",
    "randomization",
    "crf_dmp",
    "safety_reporting",
    "icf",
    "stratification",
    "regulatory_sources",
    "meta_analysis",
    "patient_population",
    "comparative_safety",
    "multiplicity",
    "trial_precedent",
)

# Top-level engine-result fields kept in the JSON context block. Anything not
# listed reaches the model through the engine's own directive instead.
_ENGINE_CONTEXT_FIELDS: dict[str, tuple[str, ...]] = {
    # Keeps `timepoints` because the Study Assessments table needs the per-row
    # cumulative_volume_ml, which the directive summarises rather than tabulates.
    "sampling_schedule": (
        "summary", "n_timepoints", "periods", "timepoints", "blood_volume",
        "tmax_hours", "tmax_basis", "t_half_hours", "t_half_basis",
        "last_timepoint_hours", "last_timepoint_label", "last_timepoint_half_lives",
        "auc_metric", "auc_extrap_pct", "auc_extrap_limit_pct",
        "auc_extrap_within_limit", "auc_extrap_basis",
        "terminal_points", "min_terminal_points_required", "lambda_z_estimable",
        "lambda_z_required", "predose_carryover_rule", "immunogenicity_timepoints",
        "steady_state_troughs", "analytes", "assumptions", "flags", "must_disclose",
    ),
    "ddi": (
        "summary", "pathways", "pathway_keys", "prohibited", "restricted", "permitted",
        "max_washout_days", "screening_history_window_days", "history_exclusion_agents",
        "post_dose_restriction_hours", "unresolved", "assumptions",
    ),
    # Grading scale and the two rule lists come from the directive; what the
    # tables need row-by-row is the discharge criteria and the halting counts.
    "safety_gating": (
        "summary", "counts", "thresholds", "discharge_criteria", "label_findings",
        "review_and_resumption", "denominator_basis", "n_dosed_planned", "assumptions",
    ),
    "bioanalytical": (
        "summary", "assay_class", "assay_class_label", "assay_class_basis", "criteria",
        "calibration", "dilution", "analytes", "parameters", "stability", "isr",
        "sample_handling", "lab_qualification", "immunogenicity", "assumptions", "flags",
    ),
    # NOTE: `unblinded_allocation`, `blinded_enrolment_log` and `reproducibility`
    # are absent BY DESIGN -- see _scrub_allocation_secrets below.
    "randomization": (
        "summary", "design", "block", "stratification", "balance", "blinding",
        "replacement", "cautions", "assumptions", "engine", "engine_version",
    ),
    # `forms`, `fields`, `edit_checks`, `pk_sample_log_rows` and `visit_schedule`
    # are what make this result ~336 KB; the scoped directive renders them.
    "crf_dmp": (
        "summary", "standards", "sdtm_domains", "washout_days", "periods", "sequences",
        "n_forms", "n_fields", "n_edit_checks", "edit_check_counts", "critical_forms",
        "downstream_analysis_coverage", "active_conditions", "detected_from_label",
        "condition_rationale", "assumptions", "flags", "must_disclose",
    ),
    "safety_reporting": (
        "summary", "regime", "conjunction", "expedited_criteria", "seriousness",
        "timelines", "parallel_obligations", "clock", "clinicaltrials_gov",
        "expectedness_reference", "safety_signals", "assumptions",
        "regional_cross_reference", "disclaimer", "scope_note",
    ),
    # The Ethics section must REFERENCE the consent form, never reproduce it, so
    # the consent body (`sections`, `risks`, `restrictions`, ...) is withheld.
    "icf": (
        "document_type", "title", "version", "regulatory_basis", "elements_coverage",
        "missing_required_elements", "pending_fields", "readability", "assumptions",
    ),
    # The "treatment" section renders `factors` as its own HTML table (label,
    # tier, magnitude, rationale, effect, note per row — kept verbatim since
    # only top-level keys are trimmed here) and quotes `summary` in prose.
    # `suppressed_factors`/`is_biologic` are kept too so a reviewer can see the
    # modality exclusion reflected in the JSON block as well as the directive.
    "stratification": ("summary", "factors", "suppressed_factors", "is_biologic"),
    # No section renders a raw regulatory-sources table — the directive is the
    # authoritative form — so this projection stays deliberately thin.
    "regulatory_sources": ("summary", "search_executed", "referenced_sources"),
    # meta_analysis_directive() already fully narrates the adopted CV, its
    # provenance, and the complete per-study table (see services/meta_analysis.py)
    # — no section renders a second, raw meta-analysis table from this key, so
    # this projection stays deliberately thin, same rationale as
    # regulatory_sources above. Critically, this is what keeps
    # cv_excluded_implausible, cv_wrong_basis_metrics/cv_wrong_basis_pct and
    # studies_excluded_wrong_population OUT of the JSON block — those are
    # exactly the values the directive deliberately tabulates-but-does-not-adopt
    # and explains in prose; leaving them in raw JSON would hand the model a
    # second, uncurated source that disagrees with the directive's disclosure.
    "meta_analysis": ("summary",),
    # Both of these are fully narrated by their own directives; the raw dicts
    # carry ranked-but-rejected candidates and per-timepoint schedules that the
    # directive already renders in the form the model should consume, so only
    # the summary line is carried as JSON. Same rationale as meta_analysis.
    "patient_population": ("summary",),
    # Fully narrated by trial_precedent_directive(), including the NCT-cited
    # excerpts of real eligibility text; the raw dict carries the full
    # (length-bounded but still sizeable) precedent-trial text a second time,
    # so only the summary line is carried as JSON — same rationale as
    # patient_population/meta_analysis above.
    "trial_precedent": ("summary",),
    "comparative_safety": ("safety_months", "doses_to_steady_state"),
    # Fully narrated by multiplicity_directive(); the raw dict adds only the
    # rationale strings the directive already renders.
    "multiplicity": ("n_co_primary", "alpha_per_endpoint", "per_endpoint_power", "overall_power"),
}

# Allocation concealment (ICH E9 s2.3.2). If the caller ever hands us the RAW
# generate_allocation() result instead of randomization.blinded_view(), the
# per-subject assignment list, the block size and the seed that regenerates the
# whole list would otherwise be serialised straight into the prompt -- and from
# there into the protocol body. These keys are stripped recursively, at every
# depth, so a shape change upstream cannot silently reopen the leak.
_ALLOCATION_SECRET_KEYS = frozenset({
    "unblinded_allocation",
    "blinded_enrolment_log",
    "entries",
    "reproducibility",
    "seed",
    "randomization_seed",
    # The block size and everything it can be arithmetically recovered from
    # (size = n_sequences x repeats_per_sequence_per_block).
    "size",
    "block_size",
    "repeats_per_sequence_per_block",
    "n_blocks_per_stream",
    # Per-subject assignment fields, wherever they appear.
    "sequence",
    "treatment_by_period",
    "randomization_number",
    "position_in_block",
})

# Nested whitelists for the randomization projection. `block` and `balance` each
# carry fields the engine itself labels RESTRICTED -- the block structure, and the
# unblinded balance statement. blinded_view() already prunes them, but the router
# wiring lives in another module, so this module does not depend on that having
# been done correctly.
_RANDOMIZATION_SUBFIELDS: dict[str, tuple[str, ...]] = {
    "block": (
        "method", "method_detail", "source", "disclosure", "disclosed",
        "protocol_body_statement", "reason", "n_sequences",
        "is_multiple_of_n_sequences",
    ),
    "balance": (
        "allocation_ratio", "sequences", "disclosable_statement",
        "counts_at_planned_enrolment", "max_imbalance_at_planned_enrolment",
        "exact_at_every_completed_block",
    ),
}


def _scrub_allocation_secrets(value: Any) -> Any:
    """Recursively drop allocation-concealment-sensitive keys from a value."""
    if isinstance(value, dict):
        return {
            k: _scrub_allocation_secrets(v)
            for k, v in value.items()
            if k not in _ALLOCATION_SECRET_KEYS
        }
    if isinstance(value, list):
        return [_scrub_allocation_secrets(v) for v in value]
    return value


def _project_engine_result(key: str, result: Any) -> Any:
    """Trim one engine result to the fields the JSON context block should carry."""
    fields = _ENGINE_CONTEXT_FIELDS.get(key)
    if not fields or not isinstance(result, dict):
        return result
    projected: dict[str, Any] = {k: result[k] for k in fields if k in result}
    if key == "randomization":
        for sub, allowed in _RANDOMIZATION_SUBFIELDS.items():
            block = projected.get(sub)
            if isinstance(block, dict):
                projected[sub] = {k: v for k, v in block.items() if k in allowed}
        projected = _scrub_allocation_secrets(projected)
    return projected


def _project_engine_context(context: dict) -> dict:
    """Return ``context`` with every known engine result replaced by its projection."""
    if not any(k in context for k in _ENGINE_CONTEXT_KEYS):
        return context
    out = dict(context)
    for key in _ENGINE_CONTEXT_KEYS:
        if key in out:
            out[key] = _project_engine_result(key, out[key])
    return out


def _render_engine_directive(key: str, renderer: Any, result: Any) -> str:
    """Render one engine directive, never letting a bad payload break generation.

    A malformed or unexpectedly-shaped engine result must degrade to "no locked
    facts for this engine" rather than failing the whole section: losing a
    directive costs precision, raising costs the document.
    """
    if not result:
        return ""
    try:
        text = renderer(result)
    except Exception:  # noqa: BLE001 - see docstring
        return ""
    return text.strip() if isinstance(text, str) else ""


def _engine_directives(section_name: str, context: dict) -> str:
    """Assemble the locked-fact blocks the given section's engines contribute.

    Sibling of _locked_design_directive(): same contract (returns "" when there
    is nothing to lock), same purpose (stop the model inventing a number a
    regulator could challenge), applied per-engine rather than to the sample
    size. Order is stable so the prompt is reproducible.
    """
    plan = _ENGINE_DIRECTIVE_PLAN.get(section_name)
    if not plan:
        return ""
    blocks = [
        block
        for key, renderer in plan
        if (block := _render_engine_directive(key, renderer, context.get(key)))
    ]
    if not blocks:
        return ""
    return "\n\n" + "\n\n".join(blocks) + "\n"


def _all_engine_directives(context: dict) -> str:
    """Every engine's locked facts, once each, for a hypothetical whole-document
    caller that wants per-section scoping collapsed into one block.

    NOT CURRENTLY CALLED ANYWHERE — services/three_stage_pipeline.py, the
    actual whole-document caller (the path POST /api/protocols uses), does
    NOT use this. It maintains its own independent, hand-maintained
    _ENGINE_DIRECTIVES tuple plus a local _engine_directives() function
    instead (different return shape: (text, rendered_keys), which this
    function does not provide — that tuple is what lets the caller drop a
    rendered engine's raw payload from the serialised JSON context, which
    this function's plain ``str`` return cannot support without further work).

    This duplication is exactly the kind of oversight that let a real engine
    (meta_analysis) go missing from three_stage_pipeline.py's tuple for a
    while, undetected, until it was found in code review — this function
    would have picked it up automatically (it derives its engine list by
    iterating _ENGINE_DIRECTIVE_PLAN.values(), so a new PLAN entry needs no
    matching manual edit here), which the hand-maintained tuple cannot. Kept
    for that reason, not because anything currently depends on it — a
    reasonable follow-up is adapting it to return (text, rendered_keys) and
    wiring it into three_stage_pipeline.py in place of the tuple, removing the
    duplication at its root rather than requiring both to be kept in sync by
    hand.
    """
    seen: set[str] = set()
    blocks: list[str] = []
    for plan in _ENGINE_DIRECTIVE_PLAN.values():
        for key, renderer in plan:
            if key in seen:
                continue
            seen.add(key)
            block = _render_engine_directive(key, renderer, context.get(key))
            if block:
                blocks.append(block)
    if not blocks:
        return ""
    return "\n\n" + "\n\n".join(blocks) + "\n"


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
