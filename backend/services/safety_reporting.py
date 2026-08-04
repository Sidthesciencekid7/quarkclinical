"""Expedited IND safety reporting plan - the sponsor's 21 CFR 312.32 obligations.

WHY THIS MODULE EXISTS
----------------------
"7 days for fatal SUSARs, 15 days for the rest" is the single most-repeated -
and most-misstated - sentence in a clinical protocol. Before this module the
platform carried it as one line of boilerplate inside an AI prompt, which means
the model was free to paraphrase it, attach it to the wrong trigger, start the
clock on the wrong date, or assert IND obligations for a study that is not run
under an IND at all. Each of those is a finding a reviewer can raise.

So the reporting plan is COMPUTED here and injected into the generation prompt as
locked facts, exactly like the sample size (:mod:`services.biostatistics`), the
five design calls (:mod:`services.design_decisions`) and the stratification
factor list (:mod:`services.stratification`). The model narrates; it never
decides which report is due or when.

THE CONJUNCTION THAT MAKES THIS CORRECT
---------------------------------------
An event is an expedited IND safety report only if it is ALL THREE of:

  1. SERIOUS      - it meets one of the outcome criteria in 21 CFR 312.32(a)
                    (death, life-threatening, inpatient hospitalisation or its
                    prolongation, persistent/significant disability or
                    incapacity, congenital anomaly, or the important-medical-
                    event catch-all).
  2. UNEXPECTED   - it is not listed in the Investigator's Brochure (or, absent
                    an IB, the reference safety information), OR it is listed
                    but is observed at a greater SPECIFICITY or SEVERITY than
                    described.
  3. SUSPECTED    - there is a reasonable possibility that the drug caused it
                    (21 CFR 312.32(a): "reasonable possibility" means there is
                    evidence to suggest a causal relationship).

AND, not OR. The 2010 IND safety reporting final rule exists largely because
sponsors were submitting every serious event regardless of causality, burying
the real signals. :func:`classify_event` encodes the conjunction in code and is
total over all sixteen truth-table combinations, so the protocol can carry the
decision table itself rather than a paragraph of prose that may or may not
reconstruct it correctly.

THE CLOCK
---------
Both clocks run in CALENDAR days (weekends and public holidays count) from the
sponsor's DATE OF INITIAL RECEIPT of the information - i.e. from sponsor
AWARENESS, not from the date the case is coded, medically reviewed, or entered
into a database. Protocols routinely - and consequentially - get this wrong.
See ``CLOCK_START_CONVENTION`` for the one place where the regulation's own
wording differs and why this plan takes the conservative reading.

APPLICABILITY - THE HONEST PART
-------------------------------
Not every study in this platform is run under an IND. A domestic ANDA
bioequivalence study of an approved, non-cytotoxic, non-radiolabelled product at
or below the labelled dose is frequently exempt from part 312 under 21 CFR
320.31(d); a 351(k) biosimilar PK-similarity study essentially always requires
one. Asserting IND obligations universally would be wrong in one direction and
asserting exemption universally would be wrong in the other, so
:func:`build_safety_reporting_plan` DETERMINES the regime, states its basis, and
lists the exemption conditions it relied on - including conditions it read off
the real DailyMed label text rather than assumed.

SCOPE
-----
This is a protocol-package artifact: the reporting PLAN plus the decision engine
that classifies a described event. It is deliberately not a safety database, an
AE intake API, an FDA transmission channel, or a running deadline clock -
those belong to a pharmacovigilance system, not to a protocol author.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Timelines
# ---------------------------------------------------------------------------
# CITED - each of these numbers is written in the regulation itself.
FATAL_OR_LIFE_THREATENING_DAYS = 7      # 21 CFR 312.32(c)(2)
OTHER_SUSAR_DAYS = 15                   # 21 CFR 312.32(c)(1)
WRITTEN_REPORT_AFTER_7DAY_DAYS = 15     # 21 CFR 312.32(c)(2) -> (c)(1) written report
FOLLOWUP_REPORT_DAYS = 15               # 21 CFR 312.32(d)(3)
IND_ANNUAL_REPORT_DAYS = 60             # 21 CFR 312.33 (within 60 days of the
                                        # anniversary of the IND taking effect)
CTGOV_REGISTRATION_DAYS = 21            # 42 CFR 11.24(a) (FDAAA 801)
CTGOV_RESULTS_DAYS = 365                # 42 CFR 11.44(a) - 1 year after the
                                        # primary completion date
EU_FOLLOWUP_ADDITIONAL_DAYS = 8         # Regulation (EU) 536/2014 Annex III:
                                        # 8 further calendar days to complete a
                                        # 7-day fatal/life-threatening report

# ASSUMED - the regulation states a duty but no number. These are the common
# operational conventions; they are surfaced in the plan's `assumptions` list so
# the protocol DISCLOSES them instead of passing them off as regulatory text.
INVESTIGATOR_TO_SPONSOR_HOURS = 24      # 21 CFR 312.64(b) / ICH E6(R2) 4.11.1
                                        # both say "immediately"; 24 hours of
                                        # investigator awareness is the standard
                                        # operational reading.
IRB_NOTIFICATION_WORKING_DAYS = 5       # 21 CFR 312.66 / 56.108(b)(1) require
                                        # "prompt" reporting of unanticipated
                                        # problems but set no deadline; most IRB
                                        # SOPs use 5-10 working days. The IRB's
                                        # own SOP governs where it is stricter.

# Rendered labels for the two clocks, kept next to the constants so the day
# count and its wording can never drift apart.
FATAL_OR_LIFE_THREATENING_DAY_LABEL = (
    f"{FATAL_OR_LIFE_THREATENING_DAYS} calendar day expedited IND safety report"
)
OTHER_SUSAR_DAY_LABEL = (
    f"{OTHER_SUSAR_DAYS} calendar day written IND safety report"
)

CLOCK_START = (
    "Day 0 is the date of the Sponsor's initial receipt of the information "
    "(the date any Sponsor personnel first become aware of the minimum criteria "
    "for a report), NOT the date of medical review, coding, or database entry."
)

# The one place the regulation's wording differs from this plan's convention.
# 312.32(c)(2) ties the 7-day clock to "the sponsor's initial receipt of the
# information". 312.32(c)(1) words the 15-day clock as running from when "the
# sponsor determines that the information qualifies for reporting". Because that
# determination must itself be made promptly, a later determination date can
# never be used to buy calendar time; fixing Day 0 at initial receipt for BOTH
# clocks is the conservative, auditable reading and matches ICH E2A day-zero
# practice. Disclosed in the plan rather than hidden.
CLOCK_START_CONVENTION = (
    "Both the 7-day and the 15-day clocks are run from the Sponsor's date of "
    "initial receipt of the information. 21 CFR 312.32(c)(1) words the 15-day "
    "clock as running from the Sponsor's determination that the information "
    "qualifies for reporting; because that determination must be made promptly, "
    "this protocol fixes Day 0 at initial receipt so that a delayed "
    "determination cannot extend the deadline."
)

CALENDAR_DAY_NOTE = (
    "All expedited deadlines are CALENDAR days: weekends and public holidays "
    "count, and a deadline falling on a non-working day is not extended."
)

SUSAR_DEFINITION = (
    "A Suspected Unexpected Serious Adverse Reaction (SUSAR) is an adverse event "
    "that is SERIOUS and UNEXPECTED and for which there is a reasonable "
    "possibility that the investigational product caused it. All three "
    "conditions must hold simultaneously."
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeriousnessCriterion:
    """One outcome criterion from the 21 CFR 312.32(a) definition of serious."""

    key: str
    label: str
    definition: str
    regulation_ref: str
    common_error: str = ""      # how protocols get this specific criterion wrong

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExpeditedCriterion:
    """One of the three independent conditions that must ALL hold."""

    key: str
    label: str
    definition: str
    regulation_ref: str
    assessed_by: str            # who owns the judgement
    common_error: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReportingDuty:
    """One obligation in the plan: who reports what, to whom, by when."""

    key: str
    label: str
    reporter: str
    recipient: str
    trigger: str
    deadline: str               # rendered for the protocol table
    deadline_value: Optional[int]
    deadline_unit: str          # calendar days | working days | hours
    clock_start: str
    method: str
    regulation_ref: str
    rationale: str
    applicable: bool = True
    applies_when: str = ""
    assumed: bool = False       # True when the NUMBER is a convention, not a
                                # figure written in the regulation

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LabelSafetySignal:
    """A serious risk detectable in the real label text, and what it implies for
    the reporting plan (expectedness baseline, AESI list, IND exemption)."""

    key: str
    label: str
    why: str
    reporting_consequence: str
    detected: bool = False
    evidence: str = ""          # the exact label phrase that fired the cue

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Seriousness - 21 CFR 312.32(a), aligned with ICH E2A II.B
# ---------------------------------------------------------------------------
# These six ARE the regulatory definition. A protocol that drops the
# important-medical-event catch-all under-reports; one that treats CTCAE
# "severe" as serious over-reports. Both are common.

_SERIOUSNESS_CRITERIA: tuple[SeriousnessCriterion, ...] = (
    SeriousnessCriterion(
        key="death",
        label="Results in death",
        definition=(
            "The event resulted in the death of the subject. The event, not the "
            "death itself, is what is reported; death is the outcome."
        ),
        regulation_ref="21 CFR 312.32(a)",
        common_error=(
            "Recording 'death' as the adverse event term rather than the event "
            "that led to it, which makes the case uninterpretable to the "
            "reviewer."
        ),
    ),
    SeriousnessCriterion(
        key="life_threatening",
        label="Life-threatening",
        definition=(
            "In the view of either the investigator or the Sponsor, the event "
            "placed the subject at IMMEDIATE risk of death as it actually "
            "occurred."
        ),
        regulation_ref="21 CFR 312.32(a)",
        common_error=(
            "Classifying an event as life-threatening because it MIGHT have "
            "caused death had it occurred in a more severe form. The regulation "
            "explicitly excludes that reading; the risk of death must attach to "
            "the event as it occurred."
        ),
    ),
    SeriousnessCriterion(
        key="hospitalisation",
        label="Inpatient hospitalisation or prolongation of existing hospitalisation",
        definition=(
            "The event required admission as an inpatient, or extended an "
            "admission that was already in progress."
        ),
        regulation_ref="21 CFR 312.32(a)",
        common_error=(
            "Counting protocol-mandated confinement, an elective procedure "
            "planned before enrolment, or a purely social/administrative "
            "admission as an SAE; and conversely failing to count a genuine "
            "prolongation of a planned admission."
        ),
    ),
    SeriousnessCriterion(
        key="disability",
        label="Persistent or significant incapacity or substantial disruption of the "
              "ability to conduct normal life functions",
        definition=(
            "The event produced a substantial, persisting disruption of the "
            "subject's capacity to carry out normal life functions."
        ),
        regulation_ref="21 CFR 312.32(a)",
        common_error=(
            "Reading 'disability' as permanent only. Persistent significant "
            "incapacity qualifies even when it eventually resolves."
        ),
    ),
    SeriousnessCriterion(
        key="congenital_anomaly",
        label="Congenital anomaly or birth defect",
        definition=(
            "A congenital anomaly or birth defect in the offspring of a subject "
            "exposed to the investigational product."
        ),
        regulation_ref="21 CFR 312.32(a)",
        common_error=(
            "Omitting the criterion because the study excludes pregnant "
            "subjects. Exposure can still occur through partner pregnancy or an "
            "undetected pregnancy at dosing, which is why the protocol needs a "
            "pregnancy-reporting pathway regardless."
        ),
    ),
    SeriousnessCriterion(
        key="important_medical_event",
        label="Important medical event (catch-all)",
        definition=(
            "An event that meets none of the outcome criteria above may still be "
            "serious when, on appropriate medical judgement, it jeopardises the "
            "subject and may require medical or surgical intervention to prevent "
            "one of those outcomes. The regulation's own examples are allergic "
            "bronchospasm requiring intensive treatment in an emergency room or "
            "at home, blood dyscrasias or convulsions that do not result in "
            "inpatient hospitalisation, and the development of drug dependency "
            "or drug abuse."
        ),
        regulation_ref="21 CFR 312.32(a); ICH E2A II.B",
        common_error=(
            "Dropping the catch-all entirely, which is the single largest source "
            "of under-reporting: an anaphylactic reaction treated successfully "
            "on site without admission meets no other criterion but is "
            "unquestionably serious."
        ),
    ),
)

SEVERITY_VS_SERIOUSNESS = (
    "Severity and seriousness are independent axes and must not be conflated. "
    "Severity is INTENSITY, graded here by CTCAE v5.0 (Grade 1-5); seriousness "
    "is a REGULATORY OUTCOME classification defined by the criteria above. A "
    "Grade 3 (severe) headache that needs no intervention is not serious, while "
    "a Grade 2 (moderate) anaphylactic reaction requiring emergency treatment "
    "is serious. Only seriousness feeds the expedited-reporting decision."
)


# ---------------------------------------------------------------------------
# The three expedited criteria - modelled as independent conditions
# ---------------------------------------------------------------------------

_EXPEDITED_CRITERIA: tuple[ExpeditedCriterion, ...] = (
    ExpeditedCriterion(
        key="serious",
        label="Serious",
        definition=(
            "The event meets at least one of the seriousness outcome criteria of "
            "21 CFR 312.32(a), including the important-medical-event catch-all."
        ),
        regulation_ref="21 CFR 312.32(a)",
        assessed_by=(
            "Investigator in the first instance; the Sponsor may upgrade "
            "seriousness but never downgrades the Investigator's assessment."
        ),
        common_error=(
            "Equating CTCAE Grade 3 'severe' with 'serious'. Intensity is not "
            "seriousness."
        ),
    ),
    ExpeditedCriterion(
        key="unexpected",
        label="Unexpected",
        definition=(
            "The event is not listed in the Investigator's Brochure - or, where "
            "no IB is required or available, is not consistent with the risk "
            "information in the general investigational plan or the current "
            "application - OR it IS listed but is observed at a greater "
            "SPECIFICITY or SEVERITY than described. The regulation's own "
            "example: hepatic necrosis is unexpected when the reference safety "
            "information describes only elevated hepatic enzymes or hepatitis. "
            "Events described only as class effects, or only as anticipated from "
            "the pharmacology of the compound, are likewise UNEXPECTED for the "
            "specific product under investigation."
        ),
        regulation_ref="21 CFR 312.32(a)",
        assessed_by=(
            "The Sponsor, against the current reference safety information. This "
            "assessment is NOT delegated to the Investigator."
        ),
        common_error=(
            "Judging expectedness against the marketed package insert when an "
            "Investigator's Brochure exists, and ignoring the "
            "specificity/severity clause so that a labelled term is treated as "
            "expected no matter how much worse the observed event is."
        ),
    ),
    ExpeditedCriterion(
        key="causally_related",
        label="Reasonable possibility that the drug caused the event "
              "(suspected adverse reaction)",
        definition=(
            "There is evidence to suggest a causal relationship between the "
            "investigational product and the event. 'Reasonable possibility' "
            "implies a lower degree of certainty than an established adverse "
            "reaction, but it does require evidence - a temporal association "
            "alone is not sufficient for a single case."
        ),
        regulation_ref="21 CFR 312.32(a) definition of 'suspected adverse reaction'",
        assessed_by=(
            "The Sponsor makes the reporting determination, taking the "
            "Investigator's causality assessment into account; either party's "
            "judgement that a reasonable possibility exists is sufficient to "
            "trigger the report."
        ),
        common_error=(
            "Submitting every serious unexpected event irrespective of "
            "causality. The 2010 IND safety reporting final rule was written "
            "specifically to stop that practice, because uninformative "
            "individual reports obscure real signals."
        ),
    ),
)

CONJUNCTION_RULE = (
    "EXPEDITED IND SAFETY REPORT = SERIOUS **AND** UNEXPECTED **AND** REASONABLE "
    "POSSIBILITY OF CAUSATION. If any one of the three fails, no expedited IND "
    "safety report is due - the event is still recorded, still reported to the "
    "Sponsor and the IRB/IEC as applicable, and still counted in the aggregate "
    "safety analyses."
)


# ---------------------------------------------------------------------------
# Label-derived safety signals
# ---------------------------------------------------------------------------
# Two jobs. (1) EXPECTEDNESS BASELINE: a serious risk that the reference safety
# information already describes is EXPECTED, so its occurrence does NOT trigger
# an expedited report unless it is observed at greater specificity or severity.
# Reading that baseline off the real DailyMed text is far more defensible than
# letting a model recall which reactions are labelled. (2) APPLICABILITY: a
# cytotoxic product cannot use the 21 CFR 320.31(d) IND exemption, and that is
# also detectable from the label.

_SIGNAL_CATALOGUE: tuple[LabelSafetySignal, ...] = (
    LabelSafetySignal(
        key="cytotoxic",
        label="Cytotoxic / antineoplastic product",
        why=(
            "A cytotoxic drug product is excluded from the bioavailability / "
            "bioequivalence IND exemption, and its expected toxicities are "
            "severe enough that the seriousness assessment is exercised "
            "constantly."
        ),
        reporting_consequence=(
            "The study must be conducted under an IND; full 21 CFR 312.32 "
            "expedited reporting applies."
        ),
    ),
    LabelSafetySignal(
        key="hepatotoxicity",
        label="Hepatotoxicity / drug-induced liver injury",
        why=(
            "DILI is the archetypal case where the specificity/severity clause "
            "bites: labelled 'transaminase elevations' do not make observed "
            "hepatic necrosis or a Hy's-Law case expected."
        ),
        reporting_consequence=(
            "Pre-specify a Hy's-Law case (ALT or AST >= 3x ULN with total "
            "bilirubin >= 2x ULN and no alternative explanation) as an IMPORTANT "
            "MEDICAL EVENT, hence serious even without hospitalisation, and "
            "define the liver-monitoring and withdrawal rules."
        ),
    ),
    LabelSafetySignal(
        key="qt_prolongation",
        label="QT/QTc prolongation or torsade de pointes",
        why=(
            "A proarrhythmic signal in a healthy-volunteer study is a "
            "disproportionate risk and drives the ECG monitoring schedule."
        ),
        reporting_consequence=(
            "List as an adverse event of special interest with pre-specified "
            "ECG stopping criteria; torsade de pointes is an important medical "
            "event and therefore serious."
        ),
    ),
    LabelSafetySignal(
        key="hypersensitivity",
        label="Serious hypersensitivity / anaphylaxis",
        why=(
            "Anaphylaxis frequently resolves after on-site treatment without "
            "admission, so it meets no outcome criterion other than the "
            "important-medical-event catch-all."
        ),
        reporting_consequence=(
            "State explicitly that anaphylaxis is serious via the "
            "important-medical-event criterion even when the subject is not "
            "hospitalised."
        ),
    ),
    LabelSafetySignal(
        key="severe_cutaneous",
        label="Severe cutaneous adverse reaction (SJS / TEN / DRESS)",
        why=(
            "Severe cutaneous reactions are life-threatening and are graded "
            "differently across systems, so they are a frequent seriousness "
            "misclassification."
        ),
        reporting_consequence=(
            "Classify as serious in every case and treat as an adverse event of "
            "special interest with immediate discontinuation."
        ),
    ),
    LabelSafetySignal(
        key="blood_dyscrasia",
        label="Blood dyscrasia (agranulocytosis, aplastic anaemia, severe neutropenia)",
        why=(
            "The regulation names blood dyscrasias that do not result in "
            "hospitalisation as an example of the important-medical-event "
            "catch-all."
        ),
        reporting_consequence=(
            "Serious via the important-medical-event criterion regardless of "
            "admission; specify haematology monitoring."
        ),
    ),
    LabelSafetySignal(
        key="seizure",
        label="Seizure / convulsion",
        why=(
            "Convulsions not resulting in inpatient hospitalisation are another "
            "of the regulation's explicit important-medical-event examples."
        ),
        reporting_consequence=(
            "Serious via the important-medical-event criterion regardless of "
            "admission."
        ),
    ),
    LabelSafetySignal(
        key="suicidality",
        label="Suicidal ideation or behaviour",
        why=(
            "A psychiatric signal requires an active surveillance instrument "
            "rather than open-ended questioning alone."
        ),
        reporting_consequence=(
            "Adverse event of special interest with a pre-specified assessment "
            "instrument; suicidal behaviour is an important medical event."
        ),
    ),
    LabelSafetySignal(
        key="embryofetal_toxicity",
        label="Embryo-fetal toxicity / teratogenicity",
        why=(
            "The congenital-anomaly seriousness criterion can only be operated "
            "if pregnancies are actively detected and followed."
        ),
        reporting_consequence=(
            "Mandate the pregnancy-reporting pathway (including partner "
            "pregnancy), contraception requirements, and follow-up of the "
            "pregnancy to outcome; a congenital anomaly in the offspring is "
            "serious by definition."
        ),
    ),
    LabelSafetySignal(
        key="immunogenicity",
        label="Immunogenicity / anti-drug antibodies",
        why=(
            "For a biological product, anti-drug antibodies drive "
            "hypersensitivity and infusion reactions and are assessed alongside "
            "PK similarity."
        ),
        reporting_consequence=(
            "List immunogenicity-associated events (hypersensitivity, infusion "
            "reactions, injection-site reactions) as adverse events of special "
            "interest and tie the ADA sampling schedule to them."
        ),
    ),
    LabelSafetySignal(
        key="dependence",
        label="Drug dependence or abuse potential",
        why=(
            "The development of drug dependency or drug abuse is an explicit "
            "important-medical-event example in the regulation."
        ),
        reporting_consequence=(
            "Serious via the important-medical-event criterion; specify "
            "controlled-substance handling and accountability."
        ),
    ),
    LabelSafetySignal(
        key="boxed_warning",
        label="Boxed warning on the reference label",
        why=(
            "A boxed warning marks the risks the reference safety information "
            "already describes, which makes them EXPECTED - the opposite of a "
            "reason to file an expedited report - while still demanding "
            "pre-specified monitoring."
        ),
        reporting_consequence=(
            "Name the boxed risks in the protocol as expected events under the "
            "reference safety information, with monitoring and stopping rules; "
            "they become reportable only at greater specificity or severity."
        ),
    ),
)

_BY_SIGNAL_KEY = {s.key: s for s in _SIGNAL_CATALOGUE}

# Signals that, when present, describe a SERIOUS risk already carried by the
# reference safety information - i.e. the expectedness baseline. (A boxed
# warning is handled separately: it is a label FEATURE, not an event term.)
_EXPECTEDNESS_SIGNALS = (
    "hepatotoxicity", "qt_prolongation", "hypersensitivity", "severe_cutaneous",
    "blood_dyscrasia", "seizure", "suicidality", "embryofetal_toxicity",
    "immunogenicity", "dependence", "cytotoxic",
)


# ---------------------------------------------------------------------------
# Applicability detection - deterministic cues from the real label text
# ---------------------------------------------------------------------------
# Matched case-insensitively against every text field of the trimmed DailyMed
# label dict already assembled for the protocol (see routers/protocols.py
# _fetch_label_safe). Scanning ALL values rather than a fixed key list means
# detection improves automatically if the boxed-warning / warnings-and-
# precautions / adverse-reactions SPL sections are added to the fetch.

_CUES: dict[str, tuple[str, ...]] = {
    "cytotoxic": (
        r"cytotoxic", r"antineoplastic", r"chemotherap",
        r"cytotoxic chemotherapy", r"myelosuppressi",
    ),
    "hepatotoxicity": (
        r"hepatotoxic", r"drug-?induced liver injury", r"\bdili\b",
        r"hepatic (?:necrosis|failure)", r"transaminase", r"\balt\b.{0,30}\buln\b",
        r"hepatitis", r"jaundice",
    ),
    "qt_prolongation": (
        r"\bqtc?\b.{0,25}prolong", r"prolong.{0,25}\bqtc?\b",
        r"torsade", r"ventricular (?:arrhythmi|tachycardi)",
    ),
    "hypersensitivity": (
        r"anaphyla", r"hypersensitivity reaction", r"angioedema",
        r"infusion[- ]related reaction",
    ),
    "severe_cutaneous": (
        r"stevens-?johnson", r"toxic epidermal necrolysis",
        r"drug reaction with eosinophilia", r"\bdress\b",
        r"acute generalized exanthematous pustulosis",
    ),
    "blood_dyscrasia": (
        r"agranulocytosis", r"aplastic an(?:a)?emia", r"pancytopenia",
        r"neutropenia", r"thrombocytopenia", r"blood dyscrasia",
    ),
    "seizure": (r"seizure", r"convulsion", r"status epilepticus"),
    "suicidality": (r"suicidal", r"suicide"),
    "embryofetal_toxicity": (
        r"embryo-?fetal", r"teratogen", r"fetal harm", r"birth defect",
        r"pregnancy category [dx]", r"contraindicated in pregnan",
    ),
    "immunogenicity": (
        r"immunogenic", r"anti-?drug antibod", r"neutrali[sz]ing antibod",
        r"antibody formation",
    ),
    "dependence": (
        r"drug dependence", r"physical dependence", r"abuse potential",
        r"\bmisuse\b", r"controlled substance",
    ),
    "boxed_warning": (r"boxed warning", r"black box warning", r"\bwarning:\s"),
}


def _label_text(label: Optional[dict[str, Any]]) -> str:
    """Every text field of the label dict, concatenated for cue matching."""
    if not label:
        return ""
    return " ".join(str(v) for v in label.values() if isinstance(v, str) and v)


def _detect_signals(text: str) -> dict[str, str]:
    """Map signal key -> the exact label phrase that fired it (the evidence).

    Returning the matched phrase rather than the pattern keeps the evidence
    field defensible: a reviewer can search the label for the same string. All
    cue patterns are ASCII literals, so the evidence is ASCII too.
    """
    if not text:
        return {}
    lowered = text.lower()
    hits: dict[str, str] = {}
    for key, patterns in _CUES.items():
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if match:
                hits[key] = match.group(0).strip()
                break
    return hits


# ---------------------------------------------------------------------------
# Regime determination - is this study run under an IND at all?
# ---------------------------------------------------------------------------
# 21 CFR 320.31 governs when a BA/BE study needs an IND. A domestic BE study of
# an approved product is exempt from part 312 provided the product is not a new
# chemical entity, is not radioactively labelled and is not cytotoxic, the study
# does not introduce a factor that significantly increases risk (21 CFR
# 312.2(b)(1)(iii) - a higher-than-labelled dose, a new route, a new population),
# and reserve samples are retained (21 CFR 320.38 / 320.63). IRB review (part 56)
# and informed consent (part 50) apply either way.
#
# A 351(k) biosimilar PK-similarity study is an investigation of a biological
# product that the sponsor is not licensed to market; the BA/BE exemption does
# not reach it, so it runs under an IND.

_IND_BY_DEFAULT_STUDY_TYPES = ("biosimilar_pk",)
_EXEMPTION_CANDIDATE_STUDY_TYPES = ("be", "food_effect")


def _ind_regime(
    study_type_key: str,
    is_ind: Optional[bool],
    signals: dict[str, str],
    *,
    is_new_chemical_entity: bool,
    radiolabeled: bool,
    healthy_volunteers: bool,
) -> dict[str, Any]:
    """Decide which regulatory regime the safety reporting plan sits in."""
    conditions: list[dict[str, Any]] = []
    breakers: list[str] = []

    if study_type_key in _EXEMPTION_CANDIDATE_STUDY_TYPES:
        cyto_evidence = signals.get("cytotoxic", "")
        checks = (
            (
                "not_new_chemical_entity",
                "The drug product does not contain a new chemical entity",
                not is_new_chemical_entity,
                "Caller-supplied study parameter" if is_new_chemical_entity
                else "Assumed: the study compares an approved reference product",
            ),
            (
                "not_radiolabeled",
                "The drug product is not radioactively labelled",
                not radiolabeled,
                "Caller-supplied study parameter" if radiolabeled
                else "Assumed: conventional non-radiolabelled BA/BE study",
            ),
            (
                "not_cytotoxic",
                "The drug product is not cytotoxic",
                not cyto_evidence,
                (f"DailyMed label text matched '{cyto_evidence}'" if cyto_evidence
                 else "No cytotoxicity cue found in the DailyMed label text"),
            ),
            (
                "no_added_risk_factor",
                "No route, dose level, or population that significantly increases "
                "the risk (21 CFR 312.2(b)(1)(iii)) - a single dose at or below "
                "the labelled dose in the labelled route",
                True,
                "Assumed from the locked design (single therapeutic label dose); "
                "confirm against the final dosing section",
            ),
            (
                "reserve_samples_retained",
                "Reserve samples of the test and reference products are retained "
                "(21 CFR 320.38 / 320.63)",
                True,
                "Protocol/clinical-site obligation; stated in the plan",
            ),
        )
        for key, requirement, met, evidence in checks:
            conditions.append({
                "key": key,
                "requirement": requirement,
                "met": bool(met),
                "evidence": evidence,
            })
            if not met:
                breakers.append(requirement)
        exemption_available = not breakers
        default_under_ind = not exemption_available
    elif study_type_key in _IND_BY_DEFAULT_STUDY_TYPES:
        exemption_available = False
        default_under_ind = True
        conditions.append({
            "key": "biological_product",
            "requirement": (
                "The 21 CFR 320.31(d) bioavailability/bioequivalence exemption "
                "does not extend to a clinical investigation of a biological "
                "product under a 351(k) biosimilar development programme"
            ),
            "met": False,
            "evidence": f"Study type '{study_type_key}'",
        })
    else:
        # Unrecognised study type: default to the CONSERVATIVE regime (assume an
        # IND is required) rather than claim an exemption we cannot establish.
        exemption_available = False
        default_under_ind = True
        conditions.append({
            "key": "study_type_unrecognised",
            "requirement": (
                "The 21 CFR 320.31(d) exemption can only be established for a "
                "bioavailability or bioequivalence study; this study type was "
                "not recognised, so no exemption is claimed"
            ),
            "met": False,
            "evidence": f"Study type '{study_type_key or '(none supplied)'}'",
        })

    if is_ind is None:
        under_ind = default_under_ind
        determined_by = "engine (study type + label evidence)"
    else:
        under_ind = bool(is_ind)
        determined_by = "caller override (sponsor regulatory affairs decision)"

    if under_ind:
        regime = "IND - 21 CFR part 312 applies in full"
        if is_ind is not None:
            basis = (
                "The Sponsor has confirmed that this study is conducted under an "
                "IND, so the full 21 CFR part 312 safety reporting obligations "
                "apply."
            )
        elif study_type_key in _IND_BY_DEFAULT_STUDY_TYPES:
            basis = (
                "A comparative PK-similarity study of a proposed biosimilar is a "
                "clinical investigation of a biological product that the sponsor "
                "is not licensed to market, so it is conducted under an IND (21 "
                "CFR 312.2(a)); the bioavailability/bioequivalence exemption of "
                "21 CFR 320.31(d) does not apply to it."
            )
        elif breakers:
            basis = (
                "This bioequivalence study cannot use the 21 CFR 320.31(d) "
                "exemption because the following condition(s) are not met: "
                + "; ".join(breakers) + "."
            )
        else:
            basis = (
                "The study type is not one for which the bioavailability / "
                "bioequivalence IND exemption of 21 CFR 320.31(d) can be "
                "established, so the plan takes the conservative position that "
                "the study is conducted under an IND. Confirm the filing status "
                "with the Sponsor's regulatory affairs function."
            )
    elif not exemption_available:
        # The caller declared "no IND" for a study whose 320.31(d) exemption the
        # engine could NOT establish. Honour the declaration - regulatory
        # affairs owns the filing decision - but never dress it up as the
        # exemption, because the exemption is exactly what is in doubt.
        regime = "Not under an IND - exemption basis NOT established by this engine"
        basis = (
            "The Sponsor has declared that this study is not conducted under an "
            "IND. The engine could not establish the 21 CFR 320.31(d) "
            "bioavailability/bioequivalence exemption for it"
            + (" because: " + "; ".join(breakers) + "." if breakers else
               ", because the exemption does not reach this study type.")
            + " The regulatory basis for proceeding without an IND must be "
            "documented by the Sponsor's regulatory affairs function before the "
            "protocol is finalised."
        )
    else:
        regime = "IND-exempt BA/BE study - 21 CFR 320.31(d)"
        basis = (
            "A bioequivalence study conducted in the United States on an "
            "approved drug product is exempt from 21 CFR part 312 where the "
            "product is not a new chemical entity, is not radioactively "
            "labelled and is not cytotoxic, the study introduces no factor that "
            "significantly increases risk, and reserve samples are retained. "
            "The expedited IND safety reporting duties of 21 CFR 312.32 "
            "therefore do NOT apply to this study; IRB review under 21 CFR part "
            "56 and informed consent under 21 CFR part 50 apply in full."
        )

    return {
        "under_ind": under_ind,
        "regime": regime,
        "basis": basis,
        "determined_by": determined_by,
        "exemption_available": exemption_available,
        "exemption_conditions": conditions,
        "population": "healthy volunteers" if healthy_volunteers else "patients",
        "caveat": (
            "This determination is the engine's reading of the study parameters "
            "and the reference label; the Sponsor's regulatory affairs function "
            "confirms the filing status before the protocol is finalised. If an "
            "IND is opened for any reason, the 21 CFR 312.32 duties below apply "
            "from that date."
        ),
        "if_not_ind_obligations": (
            "With no IND there is no FDA expedited report. Serious adverse "
            "events are still recorded, reported by the Investigator to the "
            "Sponsor, reported to the IRB as an unanticipated problem involving "
            "risk to subjects (21 CFR 56.108(b)(1)), and reported in full in the "
            "bioequivalence study report submitted in the application."
        ),
    }


# ---------------------------------------------------------------------------
# ClinicalTrials.gov - FDAAA 801 / 42 CFR part 11
# ---------------------------------------------------------------------------
# The "applicable clinical trial" definition in 42 CFR 11.10 excludes phase 1
# investigations of drugs. Bioavailability/bioequivalence studies and
# healthy-volunteer PK studies fall in that excluded class, so FDAAA 801
# registration and results submission are usually NOT triggered - stating them
# as mandatory would be as wrong as omitting them when they do apply.
#
# WHAT THE PROTOCOL GETS, AND WHAT IT DOES NOT
# --------------------------------------------
# When the study IS an applicable clinical trial the registration and results
# duties are real obligations of this protocol, so they are stated with their
# deadlines like any other duty.
#
# When it is NOT, the protocol gets NOTHING. Earlier versions emitted a
# paragraph explaining that phase 1 investigations of drug products are
# excluded from the 42 CFR 11.10 definition and that FDAAA 801 registration is
# therefore not required. A reviewing clinical pharmacologist struck it: a
# protocol does not carry a paragraph explaining why a regulation it is not
# subject to does not apply to it. That is noise, and it invites the narration
# layer to expand it. The determination is still made and still recorded in the
# machine-readable fields (`is_applicable_clinical_trial`, `determined_by`), so
# it remains auditable - there is simply no sentence for the model to lift into
# the document, and `state_in_protocol` says so explicitly.
#
# THIS IS ABOUT THE PROTOCOL ONLY. The INFORMED CONSENT FORM is a separate
# document with a separate legal basis: 21 CFR 50.25(c) prescribes a
# ClinicalTrials.gov clause for the ICF, in wording that may not be reworded.
# services/icf.py still carries that clause verbatim, and must keep doing so -
# see icf.CLINICALTRIALS_GOV_STATEMENT and icf._clinicaltrials_gov().


def _ctgov_obligation(
    study_type_key: str,
    healthy_volunteers: bool,
    override: Optional[bool],
) -> dict[str, Any]:
    if override is not None:
        is_act = bool(override)
        determined_by = "caller override"
    elif study_type_key in _EXEMPTION_CANDIDATE_STUDY_TYPES:
        is_act = False
        determined_by = "engine: bioavailability/bioequivalence study"
    else:
        # A biosimilar PK-similarity study in healthy volunteers is a phase 1
        # investigation and therefore outside the definition; the same
        # comparison run in PATIENTS is not phase 1 and is presumptively an
        # applicable clinical trial.
        is_act = not healthy_volunteers
        determined_by = "engine: phase-1 status inferred from the study population"

    if is_act:
        rationale = (
            "This study is a controlled clinical investigation of a drug subject "
            "to FDA regulation that is not a phase 1 investigation, so it is an "
            "applicable clinical trial under FDAAA 801 (42 U.S.C. 282(j)) and 42 "
            "CFR part 11."
        )
        voluntary_note = (
            "Registration is a regulatory obligation here, not a voluntary act; "
            "the IRB, the sponsor's own policy, or ICMJE journal policy may "
            "impose further conditions."
        )
        duties = [
            {
                "key": "ctgov_registration",
                "requirement": "Register the trial on ClinicalTrials.gov",
                "deadline_value": CTGOV_REGISTRATION_DAYS,
                "deadline_unit": "calendar days",
                "clock_start": "Enrolment of the first participant",
                "regulation_ref": "42 CFR 11.24(a)",
            },
            {
                "key": "ctgov_results",
                "requirement": "Submit results information",
                "deadline_value": CTGOV_RESULTS_DAYS,
                "deadline_unit": "calendar days",
                "clock_start": "Primary completion date",
                "regulation_ref": "42 CFR 11.44(a)",
            },
            {
                "key": "ctgov_adverse_events",
                "requirement": (
                    "Include the required adverse event information in the "
                    "results submission: all-cause mortality, serious adverse "
                    "events by organ system, and other (non-serious) adverse "
                    "events exceeding the 5% frequency threshold, with the "
                    "collection approach stated"
                ),
                "deadline_value": CTGOV_RESULTS_DAYS,
                "deadline_unit": "calendar days",
                "clock_start": "Primary completion date",
                "regulation_ref": "42 CFR 11.48(a)(4)",
            },
        ]
    else:
        # NOT an applicable clinical trial - so there is no obligation, and
        # therefore nothing for the protocol to say. Both prose fields are
        # deliberately EMPTY rather than carrying an explanation of why part 11
        # does not reach this study: the reasoning belongs in the module comment
        # above, not in a generated document. Leaving a sentence here is what
        # put "FDAAA 801 registration and results submission are not required
        # for this study" into real protocols.
        rationale = ""
        voluntary_note = ""
        duties = []

    return {
        "is_applicable_clinical_trial": is_act,
        "determined_by": determined_by,
        # The single gate every renderer checks. False means: emit nothing about
        # ClinicalTrials.gov, FDAAA 801 or 42 CFR part 11 into the protocol -
        # not even a sentence saying it does not apply.
        "state_in_protocol": is_act,
        "rationale": rationale,
        "duties": duties,
        "voluntary_note": voluntary_note,
    }


# ---------------------------------------------------------------------------
# Duty construction
# ---------------------------------------------------------------------------


def _expedited_duties(under_ind: bool) -> list[dict[str, Any]]:
    """The 21 CFR 312.32 expedited duties, flagged applicable or not.

    They are ALWAYS emitted, even for an IND-exempt study, so the protocol can
    say plainly why they do not apply rather than silently omitting them - and
    so the moment an IND is opened the plan is already written.
    """
    condition = "" if under_ind else (
        "Applies only if this study is conducted under an IND. This study has "
        "been determined to be IND-exempt, so this duty does not apply as "
        "written."
    )
    duties = [
        ReportingDuty(
            key="susar_7_day",
            label="7-day expedited IND safety report - fatal or life-threatening SUSAR",
            reporter="Sponsor",
            recipient="FDA (the reviewing division for the IND)",
            trigger=(
                "A suspected adverse reaction that is SERIOUS and UNEXPECTED and "
                "whose outcome is FATAL or LIFE-THREATENING"
            ),
            deadline=f"{FATAL_OR_LIFE_THREATENING_DAYS} calendar days",
            deadline_value=FATAL_OR_LIFE_THREATENING_DAYS,
            deadline_unit="calendar days",
            clock_start=CLOCK_START,
            method=(
                "As soon as possible, by telephone, facsimile or other rapid "
                "electronic means (email as agreed with the division); a complete "
                f"written IND safety report must follow within "
                f"{WRITTEN_REPORT_AFTER_7DAY_DAYS} calendar days of the same Day 0."
            ),
            regulation_ref="21 CFR 312.32(c)(2)",
            rationale=(
                "The shorter clock exists so FDA can act on a potentially "
                "catastrophic risk before the next subject is dosed."
            ),
            applicable=under_ind,
            applies_when=condition,
        ),
        ReportingDuty(
            key="susar_15_day",
            label="15-day written IND safety report - all other SUSARs",
            reporter="Sponsor",
            recipient="FDA and ALL participating investigators",
            trigger=(
                "A suspected adverse reaction that is SERIOUS and UNEXPECTED and "
                "is neither fatal nor life-threatening"
            ),
            deadline=f"{OTHER_SUSAR_DAYS} calendar days",
            deadline_value=OTHER_SUSAR_DAYS,
            deadline_unit="calendar days",
            clock_start=CLOCK_START,
            method=(
                "Written IND safety report on FDA Form 3500A (MedWatch), or a "
                "CIOMS I form for a foreign event, or an electronic ICH E2B(R3) "
                "submission where agreed with the reviewing division."
            ),
            regulation_ref="21 CFR 312.32(c)(1)(i)(A)",
            rationale=(
                "The same report discharges the duty to FDA and the duty to "
                "inform every participating investigator, which is why the "
                "investigator notification shares this clock."
            ),
            applicable=under_ind,
            applies_when=condition,
        ),
        ReportingDuty(
            key="followup_report",
            label="Follow-up IND safety report",
            reporter="Sponsor",
            recipient="FDA and all participating investigators",
            trigger=(
                "Relevant new information on a previously submitted report, OR a "
                "follow-up investigation showing that an event not initially "
                "considered reportable is in fact reportable"
            ),
            deadline=f"{FOLLOWUP_REPORT_DAYS} calendar days",
            deadline_value=FOLLOWUP_REPORT_DAYS,
            deadline_unit="calendar days",
            clock_start=(
                "Day 0 is the date the Sponsor receives the new information, or "
                "makes the determination that the event is reportable"
            ),
            method="Written follow-up IND safety report, clearly cross-referenced "
                   "to the initial report.",
            regulation_ref="21 CFR 312.32(d)(3)",
            rationale=(
                "The Sponsor must actively investigate every safety report; a "
                "case does not close because the initial report was filed."
            ),
            applicable=under_ind,
            applies_when=condition,
        ),
        ReportingDuty(
            key="increased_rate",
            label="Increased rate of occurrence of serious suspected adverse reactions",
            reporter="Sponsor",
            recipient="FDA and all participating investigators",
            trigger=(
                "An aggregate analysis showing serious suspected adverse "
                "reactions occurring at a rate above that listed in the protocol "
                "or the Investigator's Brochure"
            ),
            deadline=f"{OTHER_SUSAR_DAYS} calendar days",
            deadline_value=OTHER_SUSAR_DAYS,
            deadline_unit="calendar days",
            clock_start=CLOCK_START,
            method="Written IND safety report describing the aggregate analysis.",
            regulation_ref="21 CFR 312.32(c)(1)(iv)",
            rationale=(
                "This is why EXPECTED serious events are still counted and "
                "analysed: individually they are not expedited-reportable, but a "
                "rise in their rate is."
            ),
            applicable=under_ind,
            applies_when=condition,
        ),
        ReportingDuty(
            key="ongoing_safety_review",
            label="Ongoing review of all safety information",
            reporter="Sponsor",
            recipient="Internal - feeds every duty above",
            trigger=(
                "Continuous, from any source, foreign or domestic: the trial "
                "itself, other studies, animal or in vitro findings, the "
                "scientific literature and marketing experience"
            ),
            deadline="Promptly and continuously",
            deadline_value=None,
            deadline_unit="continuous",
            clock_start="Continuous from IND submission",
            method="Documented Sponsor safety review process.",
            regulation_ref="21 CFR 312.32(b)",
            rationale=(
                "The reporting clocks are meaningless without a review process "
                "that establishes the date of initial receipt in the first place."
            ),
            applicable=under_ind,
            applies_when=condition,
        ),
        ReportingDuty(
            key="ind_annual_report",
            label="IND annual report / DSUR",
            reporter="Sponsor",
            recipient="FDA",
            trigger="Anniversary of the date the IND went into effect",
            deadline=f"Within {IND_ANNUAL_REPORT_DAYS} days of the anniversary",
            deadline_value=IND_ANNUAL_REPORT_DAYS,
            deadline_unit="calendar days",
            clock_start="Anniversary of the IND taking effect",
            method=(
                "IND annual report, or a Development Safety Update Report "
                "prepared to ICH E2F where the Sponsor uses the DSUR format."
            ),
            regulation_ref="21 CFR 312.33; ICH E2F",
            rationale=(
                "Serious events that were expected, or not drug-related, are "
                "reported here rather than expedited - they are not discarded."
            ),
            applicable=under_ind,
            applies_when=condition,
        ),
    ]
    return [d.to_dict() for d in duties]


def _parallel_duties(under_ind: bool, blinded: bool) -> list[dict[str, Any]]:
    """The obligations that run alongside the FDA duties and that a protocol
    must state explicitly: investigator-to-sponsor, sponsor-to-investigators,
    IRB/IEC, and the blinding rule for a reportable case."""
    duties = [
        ReportingDuty(
            key="investigator_to_sponsor",
            label="Investigator notification of a serious adverse event to the Sponsor",
            reporter="Investigator",
            recipient="Sponsor (or its designated safety mailbox/fax)",
            trigger=(
                "ANY serious adverse event, irrespective of expectedness or "
                "causality - the Investigator does not filter on reportability"
            ),
            deadline=f"Immediately, and in no case later than "
                     f"{INVESTIGATOR_TO_SPONSOR_HOURS} hours",
            deadline_value=INVESTIGATOR_TO_SPONSOR_HOURS,
            deadline_unit="hours",
            clock_start=(
                "The Investigator's first awareness of the event (Day 0 for the "
                "Sponsor's own clocks starts when the Sponsor receives it)"
            ),
            method=(
                "Completed SAE report form, followed by source documentation and "
                "the causality and severity assessments."
            ),
            regulation_ref="21 CFR 312.64(b); ICH E6(R2) 4.11.1",
            rationale=(
                "The Sponsor's 7- and 15-day clocks cannot start until the site "
                "reports, so this internal deadline protects every downstream "
                "deadline."
            ),
            assumed=True,
        ),
        ReportingDuty(
            key="sponsor_to_investigators",
            label="Sponsor notification of participating investigators",
            reporter="Sponsor",
            recipient="ALL participating investigators",
            trigger=(
                "Every IND safety report, and any other finding that suggests a "
                "significant risk to subjects"
            ),
            deadline=f"{OTHER_SUSAR_DAYS} calendar days",
            deadline_value=OTHER_SUSAR_DAYS,
            deadline_unit="calendar days",
            clock_start=CLOCK_START,
            method=(
                "The IND safety report itself, distributed to every participating "
                "investigator; the Investigator's Brochure is updated when the "
                "risk profile changes."
            ),
            regulation_ref="21 CFR 312.32(c)(1); 21 CFR 312.55(b)",
            rationale=(
                "Investigators cannot re-consent subjects or apply new stopping "
                "rules to information they have not been given."
            ),
            applicable=under_ind,
            applies_when="" if under_ind else (
                "Where the study is IND-exempt the Sponsor still informs the "
                "Investigator of any new risk information affecting subject "
                "safety, without the 21 CFR 312.32 report format."
            ),
        ),
        ReportingDuty(
            key="irb_notification",
            label="IRB / IEC notification of an unanticipated problem",
            reporter="Investigator (Sponsor supports with the safety report)",
            recipient="The reviewing IRB / IEC",
            trigger=(
                "Any unanticipated problem involving risk to subjects or others, "
                "which includes every SUSAR at this site and any safety report "
                "that changes the risk-benefit assessment"
            ),
            deadline=f"Promptly - {IRB_NOTIFICATION_WORKING_DAYS} working days "
                     f"unless the IRB's own written procedures are stricter",
            deadline_value=IRB_NOTIFICATION_WORKING_DAYS,
            deadline_unit="working days",
            clock_start="The Investigator's awareness of the unanticipated problem",
            method=(
                "Written report to the IRB per its standard operating procedures, "
                "with the subject's identity coded."
            ),
            regulation_ref="21 CFR 312.66; 21 CFR 56.108(b)(1); ICH E6(R2) 4.11.1",
            rationale=(
                "The IRB is the body that can suspend or modify the study; it "
                "cannot act on information it does not have."
            ),
            assumed=True,
        ),
        ReportingDuty(
            key="pregnancy_reporting",
            label="Pregnancy reporting and follow-up",
            reporter="Investigator",
            recipient="Sponsor, then the IRB per its procedures",
            trigger=(
                "Any pregnancy in a subject or a subject's partner occurring "
                "during the study or within the protocol-defined follow-up window"
            ),
            deadline=f"{INVESTIGATOR_TO_SPONSOR_HOURS} hours to the Sponsor",
            deadline_value=INVESTIGATOR_TO_SPONSOR_HOURS,
            deadline_unit="hours",
            clock_start="The Investigator's awareness of the pregnancy",
            method=(
                "Pregnancy report form; the pregnancy is followed to outcome. A "
                "pregnancy is not in itself an adverse event, but a spontaneous "
                "abortion is always an SAE and a congenital anomaly in the "
                "offspring meets the seriousness criteria in its own right."
            ),
            regulation_ref="ICH E2A; 21 CFR 312.32(a) congenital anomaly criterion",
            rationale=(
                "Without a pregnancy pathway the congenital-anomaly seriousness "
                "criterion can never actually be operated."
            ),
            assumed=True,
        ),
        ReportingDuty(
            key="unblinding_for_report",
            label="Unblinding of a case that requires an expedited report",
            reporter="Sponsor",
            recipient="FDA (within the safety report)",
            trigger="A blinded case that meets the expedited reporting criteria",
            deadline="Before the expedited report is submitted",
            deadline_value=None,
            deadline_unit="event-driven",
            clock_start="Determination that the case is reportable",
            method=(
                "Unblind only the individual subject's assignment, through the "
                "documented emergency unblinding procedure, keeping the study "
                "team otherwise blinded; report the treatment actually received."
            ),
            regulation_ref="21 CFR 312.32(c)(1); FDA IND safety reporting guidance",
            rationale=(
                "A blinded expedited report is uninterpretable to the reviewer, "
                "but wholesale unblinding would compromise the study."
            ),
            applicable=bool(blinded),
            applies_when=(
                "" if blinded else
                "This study is open-label, so no unblinding step is required "
                "before an expedited report."
            ),
        ),
    ]
    return [d.to_dict() for d in duties]


# ---------------------------------------------------------------------------
# Date helper - calendar-day arithmetic, nothing more
# ---------------------------------------------------------------------------


def _coerce_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def due_date_from_receipt(initial_receipt: Any, days: Optional[int]) -> Optional[str]:
    """ISO due date for a CALENDAR-day clock starting at initial receipt.

    Day 0 is the date of receipt, so a 7-day report received on a Friday is due
    on the following Friday - weekends and public holidays are NOT excluded and
    do not extend the deadline. Returns None when the date cannot be parsed or
    no deadline applies, so callers never fabricate a date.
    """
    start = _coerce_date(initial_receipt)
    if start is None or days is None:
        return None
    return (start + timedelta(days=int(days))).isoformat()


# ---------------------------------------------------------------------------
# The decision engine
# ---------------------------------------------------------------------------


def classify_event(
    serious: bool,
    unexpected: bool,
    causally_related: bool,
    fatal_or_life_threatening: bool = False,
    *,
    under_ind: bool = True,
    initial_receipt_date: Any = None,
) -> dict[str, Any]:
    """Classify one described event and compute the resulting obligation.

    The three criteria are independent and must ALL hold for an expedited IND
    safety report; the function is total over every combination of the four
    flags.

    Parameters
    ----------
    serious:
        The event meets at least one seriousness criterion of 21 CFR 312.32(a).
    unexpected:
        Not listed in the Investigator's Brochure / reference safety information
        at the observed specificity or severity.
    causally_related:
        A reasonable possibility that the investigational product caused it.
    fatal_or_life_threatening:
        The outcome was death, or the event placed the subject at immediate risk
        of death as it occurred. Selects the 7-day clock over the 15-day clock.
    under_ind:
        Whether the study is conducted under an IND. When False no FDA expedited
        report exists, but ``would_be_report_type`` still records what the
        obligation WOULD be, so the classification is not silently lost.
    initial_receipt_date:
        Optional ISO date (or date/datetime) of the Sponsor's initial receipt.
        When supplied, ``due_date`` is computed in calendar days from it.

    Returns
    -------
    A JSON-serialisable dict with the report type, day count, clock start,
    per-criterion detail and the reasoning.
    """
    notes: list[str] = []

    # An event that is fatal or life-threatening satisfies the 'death' or
    # 'life-threatening' seriousness criterion by definition. If a caller passes
    # an inconsistent combination we UPGRADE seriousness rather than let an
    # input error suppress a 7-day report - the obligation is never downgraded
    # on inconsistent input.
    serious_effective = bool(serious)
    if fatal_or_life_threatening and not serious:
        serious_effective = True
        notes.append(
            "Input inconsistency corrected: an event that is fatal or "
            "life-threatening meets the 'death' or 'life-threatening' "
            "seriousness criterion of 21 CFR 312.32(a) by definition, so "
            "seriousness was set to True. An obligation is never downgraded on "
            "an inconsistent input."
        )

    met = {
        "serious": bool(serious_effective),
        "unexpected": bool(unexpected),
        "causally_related": bool(causally_related),
    }
    criteria = []
    for crit in _EXPEDITED_CRITERIA:
        row = crit.to_dict()
        row["met"] = met[crit.key]
        criteria.append(row)
    failed = [c["key"] for c in criteria if not c["met"]]

    is_susar = all(met.values())

    if is_susar and fatal_or_life_threatening:
        would_be = (
            f"{FATAL_OR_LIFE_THREATENING_DAY_LABEL} - fatal or life-threatening "
            "SUSAR"
        )
        days: Optional[int] = FATAL_OR_LIFE_THREATENING_DAYS
        ref = "21 CFR 312.32(c)(2)"
        additional = [
            {
                "key": "written_report",
                "requirement": (
                    "Complete written IND safety report following the initial "
                    "rapid notification"
                ),
                "deadline_value": WRITTEN_REPORT_AFTER_7DAY_DAYS,
                "deadline_unit": "calendar days",
                "clock_start": CLOCK_START,
                "regulation_ref": "21 CFR 312.32(c)(1) and (c)(2)",
            },
            {
                "key": "followup",
                "requirement": "Relevant follow-up information as it becomes available",
                "deadline_value": FOLLOWUP_REPORT_DAYS,
                "deadline_unit": "calendar days",
                "clock_start": "Sponsor receipt of the follow-up information",
                "regulation_ref": "21 CFR 312.32(d)(3)",
            },
        ]
    elif is_susar:
        would_be = f"{OTHER_SUSAR_DAY_LABEL} - serious, unexpected suspected adverse reaction"
        days = OTHER_SUSAR_DAYS
        ref = "21 CFR 312.32(c)(1)(i)(A)"
        additional = [
            {
                "key": "followup",
                "requirement": "Relevant follow-up information as it becomes available",
                "deadline_value": FOLLOWUP_REPORT_DAYS,
                "deadline_unit": "calendar days",
                "clock_start": "Sponsor receipt of the follow-up information",
                "regulation_ref": "21 CFR 312.32(d)(3)",
            },
        ]
    else:
        would_be = "No expedited IND safety report"
        days = None
        ref = "21 CFR 312.32(c)(1)(i)(A) - criteria not all met"
        additional = []

    expedited = bool(is_susar and under_ind)
    if is_susar and not under_ind:
        report_type = (
            "No IND safety report - this study is not conducted under an IND"
        )
        days_out: Optional[int] = None
        notes.append(
            "The event meets all three expedited criteria, but with no IND there "
            "is no FDA expedited report. It is handled as an unanticipated "
            "problem: reported to the Sponsor, reported to the IRB, and included "
            "in the study report. If an IND is opened, the "
            f"{would_be} applies."
        )
    else:
        report_type = would_be
        days_out = days

    routine = _routine_handling(failed, met, under_ind)
    reasoning = _reasoning(criteria, is_susar, fatal_or_life_threatening, under_ind)

    return {
        "inputs": {
            "serious": bool(serious),
            "unexpected": bool(unexpected),
            "causally_related": bool(causally_related),
            "fatal_or_life_threatening": bool(fatal_or_life_threatening),
            "under_ind": bool(under_ind),
        },
        "is_serious": bool(serious_effective),
        "is_unexpected": bool(unexpected),
        "is_suspected_adverse_reaction": bool(causally_related),
        "is_susar": is_susar,
        "expedited_ind_report_required": expedited,
        "report_type": report_type,
        "would_be_report_type": would_be,
        "deadline_days": days_out,
        "deadline_unit": "calendar days" if days_out else "not applicable",
        "clock_start": CLOCK_START if days_out else "not applicable",
        "calendar_day_note": CALENDAR_DAY_NOTE,
        "initial_receipt_date": (
            _coerce_date(initial_receipt_date).isoformat()
            if _coerce_date(initial_receipt_date) else None
        ),
        "due_date": due_date_from_receipt(initial_receipt_date, days_out),
        "criteria": criteria,
        "failed_criteria": failed,
        "regulation_ref": ref,
        "additional_reports": additional,
        "routine_handling": routine,
        "reasoning": reasoning,
        "notes": notes,
    }


def _routine_handling(
    failed: list[str], met: dict[str, bool], under_ind: bool
) -> str:
    """What happens to an event that is NOT expedited-reportable - which is most
    of them, and where under-specified protocols leak obligations."""
    base = (
        "Record in the CRF with severity (CTCAE v5.0), causality and outcome, "
        "and follow to resolution."
    )
    if not failed:
        return (
            base + " In addition to the expedited report, the case is included "
            "in the aggregate safety analyses and the annual report / DSUR."
        ) if under_ind else (
            base + " Report to the IRB as an unanticipated problem and include "
            "in the study report submitted with the application."
        )
    parts = [base]
    if not met["serious"]:
        parts.append(
            "The event is not serious, so no SAE pathway is triggered; it is "
            "summarised in the study report and, where applicable, the annual "
            "report / DSUR."
        )
    else:
        parts.append(
            "The event IS serious, so the Investigator reports it to the Sponsor "
            f"within {INVESTIGATOR_TO_SPONSOR_HOURS} hours and the IRB is "
            "notified per its procedures, even though no expedited FDA report is "
            "due."
        )
    if met["serious"] and not met["unexpected"]:
        parts.append(
            "Because it is EXPECTED under the reference safety information it is "
            "not individually expedited, but it is counted in the aggregate "
            "analysis of serious suspected adverse reactions - a rise in its "
            "rate above the rate in the protocol or Investigator's Brochure is "
            "itself reportable within "
            f"{OTHER_SUSAR_DAYS} calendar days (21 CFR 312.32(c)(1)(iv))."
        )
    if met["serious"] and met["unexpected"] and not met["causally_related"]:
        parts.append(
            "Because there is no reasonable possibility that the drug caused it, "
            "it is not a suspected adverse reaction and is therefore not "
            "individually expedited; the alternative cause must be documented, "
            "and the case still contributes to the aggregate safety review "
            "(21 CFR 312.32(b))."
        )
    return " ".join(parts)


def _reasoning(
    criteria: list[dict[str, Any]],
    is_susar: bool,
    fatal: bool,
    under_ind: bool,
) -> str:
    checks = "; ".join(
        f"{c['label'].split(' (')[0]}: {'YES' if c['met'] else 'NO'}"
        for c in criteria
    )
    if is_susar and under_ind:
        clock = (
            f"{FATAL_OR_LIFE_THREATENING_DAYS} calendar days because the outcome "
            "is fatal or life-threatening"
            if fatal else
            f"{OTHER_SUSAR_DAYS} calendar days"
        )
        return (
            f"All three criteria are met ({checks}), so the event is a SUSAR and "
            f"an expedited IND safety report is due within {clock} of the "
            "Sponsor's initial receipt of the information."
        )
    if is_susar:
        return (
            f"All three criteria are met ({checks}), but the study is not "
            "conducted under an IND, so no FDA expedited report exists; the "
            "event is handled as an unanticipated problem."
        )
    return (
        f"The conjunction fails ({checks}), so this is NOT an expedited IND "
        "safety report. All three criteria must hold simultaneously."
    )


def decision_table(under_ind: bool = True) -> list[dict[str, Any]]:
    """The truth table, one row per combination - a table a protocol can print,
    so the reader never has to reconstruct the conjunction from prose.

    ``classify_event`` is total over all sixteen combinations, but the four
    where an event is fatal or life-threatening yet marked not-serious are
    logically impossible (fatal IS a seriousness criterion) and are omitted
    here: a printed protocol table must not show the reader a row that cannot
    occur. They remain classifiable, and are normalised upward, if a caller
    ever passes them.
    """
    rows: list[dict[str, Any]] = []
    for serious in (True, False):
        for unexpected in (True, False):
            for related in (True, False):
                for fatal in (True, False):
                    if fatal and not serious:
                        continue
                    out = classify_event(
                        serious, unexpected, related, fatal, under_ind=under_ind
                    )
                    rows.append({
                        "serious": serious,
                        "unexpected": unexpected,
                        "causally_related": related,
                        "fatal_or_life_threatening": fatal,
                        "expedited_ind_report_required": out[
                            "expedited_ind_report_required"],
                        "report_type": out["report_type"],
                        "deadline_days": out["deadline_days"],
                    })
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_safety_reporting_plan(
    study_type: str,
    is_ind: Optional[bool] = None,
    org_name: Optional[str] = None,
    *,
    drug_inn: Optional[str] = None,
    label: Optional[dict[str, Any]] = None,
    healthy_volunteers: bool = True,
    guideline_version: Optional[str] = None,
    is_applicable_clinical_trial: Optional[bool] = None,
    is_new_chemical_entity: bool = False,
    radiolabeled: bool = False,
    blinded: Optional[bool] = None,
) -> dict[str, Any]:
    """Build the full expedited safety reporting plan for THIS study.

    Parameters
    ----------
    study_type:
        'BE' | 'food_effect' | 'biosimilar_pk'. Drives the IND determination and
        the ClinicalTrials.gov applicability determination.
    is_ind:
        Explicit override from the Sponsor's regulatory affairs function. When
        None the engine determines the regime from the study type, the study
        parameters and the real label text, and says so in ``determined_by``.
    org_name:
        Sponsor name for the narrative; falls back to "The Sponsor".
    label:
        The trimmed DailyMed label dict already assembled for the protocol
        (``_fetch_label_safe``). Every text field is scanned for serious-risk
        cues, which supply BOTH the expectedness baseline and the cytotoxicity
        check that can defeat the BA/BE IND exemption.
    healthy_volunteers / blinded:
        Population and blinding, from the locked design decisions. ``blinded``
        defaults to True for a biosimilar PK-similarity study and False for a BE
        study, matching the conventional design.
    guideline_version:
        When it names EMA or ICH, the plan adds the parallel EU/ICH obligations
        as a cross-reference (the same 7/15 calendar-day clocks).

    Returns
    -------
    A JSON-serialisable dict, safe to persist inside population_config (JSONB).
    """
    st_key = (study_type or "").strip().lower()
    sponsor = (org_name or "").strip() or "The Sponsor"
    text = _label_text(label)
    detected = _detect_signals(text)

    if blinded is None:
        # Biosimilar PK-similarity studies are conventionally double-blind;
        # small-molecule BE studies are conventionally open-label.
        blinded = st_key == "biosimilar_pk"

    regime = _ind_regime(
        st_key,
        is_ind,
        detected,
        is_new_chemical_entity=is_new_chemical_entity,
        radiolabeled=radiolabeled,
        healthy_volunteers=healthy_volunteers,
    )
    under_ind = regime["under_ind"]

    signals: list[dict[str, Any]] = []
    for sig in _SIGNAL_CATALOGUE:
        evidence = detected.get(sig.key, "")
        enriched = LabelSafetySignal(
            **{**asdict(sig), "detected": bool(evidence), "evidence": evidence}
        ).to_dict()
        if enriched["detected"]:
            signals.append(enriched)

    labelled_risks = [
        {"key": s["key"], "label": s["label"], "evidence": s["evidence"]}
        for s in signals if s["key"] in _EXPECTEDNESS_SIGNALS
    ]

    expectedness = {
        "primary_source": (
            "The current Investigator's Brochure, where one exists for the "
            "investigational product."
        ),
        "fallback_source": (
            f"The approved US package insert for {drug_inn or 'the reference product'} "
            "(DailyMed SPL) where no Investigator's Brochure is required."
        ),
        "label_text_available": bool(text),
        "labelled_serious_risks": labelled_risks,
        "rule": (
            "A serious reaction described in the reference safety information is "
            "EXPECTED, so its occurrence does NOT trigger an expedited IND safety "
            "report - UNLESS it is observed at a greater SPECIFICITY or SEVERITY "
            "than described (21 CFR 312.32(a)), in which case it is UNEXPECTED "
            "and reportable."
        ),
        "class_effect_rule": (
            "A reaction described only as a class effect, or only as anticipated "
            "from the pharmacology of the compound, is UNEXPECTED for the "
            "specific product under investigation."
        ),
        "caveat": (
            "This list is derived from cues found in the reference label text and "
            "is a starting point for the expectedness assessment, not a "
            "substitute for the reference safety information itself."
        ),
    }

    assumptions = [
        {
            "key": "clock_start_convention",
            "value": "Day 0 = Sponsor's date of initial receipt for both clocks",
            "basis": CLOCK_START_CONVENTION,
            "disclose_in_protocol": True,
        },
        {
            "key": "investigator_to_sponsor_hours",
            "value": INVESTIGATOR_TO_SPONSOR_HOURS,
            "basis": (
                "21 CFR 312.64(b) and ICH E6(R2) 4.11.1 require immediate "
                "reporting but state no number; 24 hours of Investigator "
                "awareness is the standard operational reading."
            ),
            "disclose_in_protocol": True,
        },
        {
            "key": "irb_notification_working_days",
            "value": IRB_NOTIFICATION_WORKING_DAYS,
            "basis": (
                "21 CFR 312.66 and 56.108(b)(1) require prompt reporting of "
                "unanticipated problems but set no deadline; 5 working days is "
                "the common IRB SOP convention. The reviewing IRB's own written "
                "procedures govern where they are stricter."
            ),
            "disclose_in_protocol": True,
        },
        {
            "key": "blinding",
            "value": "double-blind" if blinded else "open-label",
            "basis": (
                "Taken from the locked design; determines whether a case must be "
                "unblinded before an expedited report is submitted."
            ),
            "disclose_in_protocol": False,
        },
    ]

    cross_ref: list[dict[str, str]] = []
    gv = (guideline_version or "").upper()
    if "EMA" in gv or "ICH" in gv or "M13" in gv:
        cross_ref.append({
            "region": "European Union",
            "obligation": (
                "Regulation (EU) 536/2014 Article 42 imposes the same expedited "
                "clocks on the sponsor for SUSARs occurring in an EU trial: 7 "
                "calendar days for fatal or life-threatening SUSARs (with "
                f"{EU_FOLLOWUP_ADDITIONAL_DAYS} further calendar days to complete "
                "the report) and 15 calendar days for all other SUSARs, submitted "
                "electronically to the EudraVigilance clinical trial module."
            ),
            "reference": "Regulation (EU) 536/2014 Art. 42 and Annex III",
        })
        cross_ref.append({
            "region": "ICH",
            "obligation": (
                "ICH E2A defines the same expedited concept and the same "
                "7/15-calendar-day standards; ICH E2B(R3) defines the electronic "
                "transmission format and ICH E2F the Development Safety Update "
                "Report."
            ),
            "reference": "ICH E2A; ICH E2B(R3); ICH E2F",
        })

    plan: dict[str, Any] = {
        "study_type": study_type,
        "sponsor": sponsor,
        "drug_inn": drug_inn,
        "regime": regime,
        "conjunction": {
            "rule": CONJUNCTION_RULE,
            "criteria_required": [c.key for c in _EXPEDITED_CRITERIA],
            "all_required": True,
            "operator": "AND",
        },
        "expedited_criteria": [c.to_dict() for c in _EXPEDITED_CRITERIA],
        "seriousness": {
            "definition_ref": "21 CFR 312.32(a); ICH E2A II.B",
            "criteria": [c.to_dict() for c in _SERIOUSNESS_CRITERIA],
            "severity_vs_seriousness": SEVERITY_VS_SERIOUSNESS,
            "susar_definition": SUSAR_DEFINITION,
        },
        "timelines": _expedited_duties(under_ind),
        "parallel_obligations": _parallel_duties(under_ind, bool(blinded)),
        "clock": {
            "start": CLOCK_START,
            "convention": CLOCK_START_CONVENTION,
            "calendar_days": True,
            "note": CALENDAR_DAY_NOTE,
        },
        "decision_table": decision_table(under_ind),
        "clinicaltrials_gov": _ctgov_obligation(
            st_key, healthy_volunteers, is_applicable_clinical_trial),
        "expectedness_reference": expectedness,
        "safety_signals": signals,
        "assumptions": assumptions,
        "regional_cross_reference": cross_ref,
        "disclaimer": (
            "A safety report or other information submitted by the Sponsor does "
            "not necessarily reflect a conclusion by the Sponsor or by FDA that "
            "the report or information constitutes an admission that the drug "
            "caused or contributed to the adverse event (21 CFR 312.32(e))."
        ),
        "scope_note": (
            "This is the reporting PLAN as it appears in the protocol package. "
            "Execution - case intake, medical review, database entry and "
            "transmission - is performed in the Sponsor's pharmacovigilance "
            "system under its own standard operating procedures."
        ),
    }
    plan["summary"] = _summary(plan, blinded=bool(blinded))
    return plan


def _summary(plan: dict[str, Any], *, blinded: bool) -> str:
    """One-paragraph plain statement for the protocol narrative."""
    regime = plan["regime"]
    sponsor = plan["sponsor"]
    if regime["under_ind"]:
        lead = (
            f"{sponsor} conducts this study under an IND, so the expedited "
            "safety reporting requirements of 21 CFR 312.32 apply in full. An "
            "event is an expedited IND safety report only when it is serious "
            "AND unexpected AND there is a reasonable possibility that the "
            "investigational product caused it; if any one of the three fails, "
            "no expedited report is due. Fatal or life-threatening suspected "
            f"unexpected serious adverse reactions are reported within "
            f"{FATAL_OR_LIFE_THREATENING_DAYS} calendar days and all other "
            f"SUSARs within {OTHER_SUSAR_DAYS} calendar days, both counted from "
            "the Sponsor's date of initial receipt of the information."
        )
    else:
        # Two very different reasons a study can be "not under an IND", and the
        # summary is the line the generation prompt narrates - so it must not
        # claim the 320.31(d) exemption when the engine could NOT establish it
        # (the caller-declared case). Asserting an exemption we did not verify
        # is exactly the error this module exists to prevent, and the regime
        # block already says so; the summary must agree with it.
        if regime.get("exemption_available"):
            opening = (
                "This study is exempt from 21 CFR part 312 under 21 CFR "
                "320.31(d), so no IND safety reports are submitted to FDA."
            )
        else:
            opening = (
                "The Sponsor has declared that this study is not conducted "
                "under an IND, so no IND safety reports are submitted to FDA. "
                "This plan does NOT establish the 21 CFR 320.31(d) "
                "bioavailability/bioequivalence exemption for it; the "
                "regulatory basis for proceeding without an IND must be "
                "documented by the Sponsor's regulatory affairs function "
                "before the protocol is finalised."
            )
        lead = (
            f"{opening} {sponsor} still "
            "operates the full serious-adverse-event pathway: the Investigator "
            f"notifies the Sponsor within {INVESTIGATOR_TO_SPONSOR_HOURS} hours, "
            "the IRB is notified of unanticipated problems involving risk to "
            "subjects, every event is recorded and followed to resolution, and "
            "all safety data are reported in the study report submitted with the "
            "application. The 21 CFR 312.32 clocks are stated in this protocol "
            "so that they take effect immediately if an IND is opened."
        )
    signals = plan.get("safety_signals") or []
    signal_tail = ""
    if signals:
        names = "; ".join(s["label"] for s in signals[:3])
        signal_tail = (
            f" The reference label carries the following risks that shape the "
            f"expectedness assessment and the monitoring plan: {names}."
        )
    ct = plan["clinicaltrials_gov"]
    # Only a real obligation earns a sentence. When the study is not an
    # applicable clinical trial the tail is EMPTY - the summary is the line the
    # generation prompt narrates, so a sentence here becomes a sentence in the
    # protocol, and "registration is not required" is exactly the noise a
    # reviewing pharmacologist struck. See _ctgov_obligation().
    ct_tail = (
        " The study is an applicable clinical trial, so it is registered on "
        f"ClinicalTrials.gov within {CTGOV_REGISTRATION_DAYS} days of first "
        "enrolment and results are submitted within one year of the primary "
        "completion date."
        if ct.get("state_in_protocol") else ""
    )
    blind_tail = (
        " A case requiring an expedited report is unblinded for that subject "
        "only, through the documented emergency procedure."
        if blinded else ""
    )
    return lead + signal_tail + ct_tail + blind_tail


def safety_reporting_directive(plan: dict[str, Any]) -> str:
    """Render the locked reporting plan as prompt text for the generator.

    Mirrors ``stratification.stratification_directive`` and
    ``design_decisions.design_directive``: these are settled facts to narrate,
    never a menu for the model to re-decide. Empty input yields an empty string
    rather than a misleading header.
    """
    if not plan or not plan.get("regime"):
        return ""
    regime = plan["regime"]
    under_ind = regime.get("under_ind")
    lines = [
        "SAFETY REPORTING PLAN - these obligations are ALREADY DETERMINED. "
        "Narrate them EXACTLY; do not add, drop, or re-derive a deadline, and "
        "never state a day count that does not appear here.",
        f"Regulatory regime: {regime.get('regime', '')} - {regime.get('basis', '')}",
        f"Summary: {plan.get('summary', '')}",
        "",
        "THE EXPEDITED REPORTING TEST (state it as a conjunction, not a list of "
        "alternatives):",
        f"  {plan.get('conjunction', {}).get('rule', CONJUNCTION_RULE)}",
    ]
    for crit in plan.get("expedited_criteria", []):
        lines.append(
            f"  - {crit['label']}: {crit['definition']} "
            f"[{crit['regulation_ref']}] Assessed by: {crit['assessed_by']}"
        )
    lines.append("")
    lines.append("SERIOUSNESS CRITERIA - reproduce all of them, including the "
                 "important-medical-event catch-all:")
    for crit in plan.get("seriousness", {}).get("criteria", []):
        lines.append(f"  - {crit['label']}: {crit['definition']}")
    lines.append(f"  {plan.get('seriousness', {}).get('severity_vs_seriousness', '')}")
    lines.append("")

    applicable = [d for d in plan.get("timelines", []) if d.get("applicable")]
    not_applicable = [d for d in plan.get("timelines", []) if not d.get("applicable")]
    if applicable:
        lines.append("REPORTING DEADLINES - use these EXACT figures everywhere a "
                     "timeline is mentioned:")
        for duty in applicable:
            lines.append(
                f"  - {duty['label']}: {duty['deadline']} "
                f"(clock start: {duty['clock_start'].rstrip('.')}). "
                f"Trigger: {duty['trigger']}. "
                f"Method: {duty['method']} [{duty['regulation_ref']}]"
            )
    if not_applicable:
        lines.append(
            "DOES NOT APPLY TO THIS STUDY - state plainly why, do NOT present "
            "these as obligations of this protocol:"
        )
        for duty in not_applicable:
            lines.append(f"  - {duty['label']}: {duty['applies_when']}")
        lines.append(f"  {regime.get('if_not_ind_obligations', '')}")
    lines.append("")

    lines.append("PARALLEL OBLIGATIONS - each must appear in the safety section:")
    for duty in plan.get("parallel_obligations", []):
        if duty.get("applicable"):
            lines.append(
                f"  - {duty['label']}: {duty['reporter']} to {duty['recipient']}, "
                f"{duty['deadline']}. Trigger: {duty['trigger']} "
                f"[{duty['regulation_ref']}]"
            )
        elif duty.get("applies_when"):
            # Still stated, because "this does not apply and here is why" is
            # information the protocol needs - silently dropping it invites the
            # model to invent a procedure (e.g. an unblinding step in an
            # open-label study).
            lines.append(f"  - {duty['label']}: {duty['applies_when']}")

    # ClinicalTrials.gov reaches the prompt ONLY when registration is a real
    # obligation of this study. When it is not, the directive says nothing about
    # it except a suppression instruction - a protocol does not explain why a
    # regulation it is not subject to does not apply. The suppression line is
    # needed because the model will otherwise reach for the boilerplate on its
    # own; it is an instruction, not text to render.
    ct = plan.get("clinicaltrials_gov", {})
    lines.append("")
    if ct.get("state_in_protocol"):
        lines.append(f"ClinicalTrials.gov: {ct.get('rationale', '')}")
        for duty in ct.get("duties", []):
            lines.append(
                f"  - {duty['requirement']}: within {duty['deadline_value']} "
                f"{duty['deadline_unit']} of {duty['clock_start']} "
                f"[{duty['regulation_ref']}]"
            )
    else:
        lines.append(
            "PUBLIC TRIAL REGISTRY - WRITE NOTHING: this study carries no "
            "ClinicalTrials.gov registration or results-submission obligation. "
            "Do NOT mention ClinicalTrials.gov, FDAAA 801, 42 CFR part 11, the "
            "phrase 'applicable clinical trial', or trial registration anywhere "
            "in the protocol - not even a sentence stating that they do not "
            "apply. (The separate INFORMED CONSENT FORM keeps its own 21 CFR "
            "50.25(c) clause; that is a different document and is unaffected.)"
        )

    expect = plan.get("expectedness_reference", {})
    if expect:
        lines.append("")
        lines.append(f"EXPECTEDNESS: {expect.get('rule', '')} "
                     f"{expect.get('class_effect_rule', '')}")
        risks = expect.get("labelled_serious_risks", [])
        if risks:
            lines.append(
                "  Serious risks already described in the reference safety "
                "information (therefore EXPECTED unless seen at greater "
                "specificity or severity): "
                + "; ".join(r["label"] for r in risks)
            )

    signals = plan.get("safety_signals", [])
    if signals:
        lines.append("")
        lines.append("LABEL-DERIVED SAFETY SIGNALS - address each one in the "
                     "monitoring and reporting text:")
        for sig in signals:
            lines.append(
                f"  - {sig['label']} (label evidence: '{sig['evidence']}'): "
                f"{sig['reporting_consequence']}"
            )

    cross_ref = plan.get("regional_cross_reference", [])
    if cross_ref:
        lines.append("")
        lines.append("PARALLEL REGIONAL OBLIGATIONS - state them as such; do NOT "
                     "merge them with the FDA timelines:")
        for item in cross_ref:
            lines.append(
                f"  - {item['region']}: {item['obligation']} [{item['reference']}]"
            )

    assumptions = plan.get("assumptions", [])
    disclosable = [a for a in assumptions if a.get("disclose_in_protocol")]
    if disclosable:
        lines.append("")
        lines.append("STATED CONVENTIONS - disclose these as protocol "
                     "conventions, not as regulatory text:")
        for item in disclosable:
            lines.append(f"  - {item['key']} = {item['value']}: {item['basis']}")

    lines.append("")
    lines.append(f"Include this disclaimer verbatim in substance: {plan.get('disclaimer', '')}")
    return "\n".join(lines)
