"""Pre-specified safety gating - AE grading, stopping/halting rules, discharge.

WHY THIS MODULE EXISTS
----------------------
ICH E6(R2) requires a protocol to state, BEFORE first-subject-in, when an
individual subject stops receiving study drug (6.4.7 subject withdrawal
criteria) and when the trial itself is suspended (4.12 premature termination or
suspension). A unit that discovers mid-study that its protocol says only
"the Investigator will monitor safety" cannot defensibly pause dosing, cannot
defensibly continue dosing, and cannot defensibly discharge a subject - every
one of those becomes an unrecorded judgement call that an inspector will read
as a protocol deviation.

Until this module existed the generator only asked the model to narrate the
CTCAE severity scale in prose. There was no countable halting boundary and no
discharge criterion anywhere in the product. Those are exactly the kind of
numbers a regulator can challenge, so - per the architectural rule this codebase
is built on - they are COMPUTED HERE and injected into the prompt as locked
facts the model may narrate but never invent, re-derive, or soften.

THE DISTINCTION THAT MAKES THIS CORRECT
---------------------------------------
Conflating the two levels of gating is the classic protocol error, and it is
dangerous in both directions:

* **INDIVIDUAL stopping rules** act on ONE subject. They answer "does this
  person receive any more study drug?" They never, by themselves, stop the
  study. A protocol that halts the whole study on any single Grade 3 event in a
  60-subject study will halt for something that was always going to happen; a
  protocol that treats an individual withdrawal as sufficient never notices a
  pattern.

* **COHORT / STUDY halting rules** act on the TRIAL. They answer "may any
  further subject be dosed?" They are cumulative counts over dosed subjects,
  and they must be scaled to the planned N - a threshold tuned for n=12 is
  simply the wrong threshold at n=60. Resumption is never automatic: it needs a
  documented joint Sponsor/Investigator review and IRB/IEC notification.

The one exception to count-scaling is deliberate and is pinned by a test: a
single related serious adverse event - which by definition includes any related
Grade 4 (life-threatening) or Grade 5 (fatal) event - or a confirmed Hy's Law
case halts dosing IMMEDIATELY at any N. A fraction rule that would tolerate
several life-threatening events before halting in a large study is not a safety
rule, it is an arithmetic accident.

EACH TRIGGER IS STATED ONCE, AT THE LEVEL WHERE IT ACTS
-------------------------------------------------------
A reviewing pharmacologist read a generated biosimilar protocol and found the
stopping rules repetitive: the same clinical event appeared as several rules
that differed only in wording. Repetition is not a cosmetic problem in a safety
section - a monitor who has to reconcile three rows to make one decision at
3 a.m. is being slowed down by the document, and a reviewer who learns that the
rules restate each other starts skimming the ones that do not.

Two mechanisms keep it out:

* STRUCTURAL. Where one trigger is a strict subset of another AND the action is
  the same, the catalogue carries a single rule whose trigger names both limbs.
  Grade 4 IS "life-threatening" and Grade 5 IS "death", which are two of the six
  ICH E2A seriousness criteria, so a separate "Grade 4/5" withdrawal rule and a
  separate "serious adverse event" withdrawal rule were one rule written twice.
  They are now one rule at each level (``ind_sae``, ``coh_related_sae``).

* CONDITIONAL (``_RULE_MERGES``). Where the overlap only exists for some
  studies, the narrower rule is dropped at assembly time IF the broader one is
  actually present, and the drop is recorded in ``suppressed_rules`` with its
  reason. Anaphylaxis is serious by definition, so a standalone cohort
  anaphylaxis halt duplicates the related-SAE halt - but only in a parenteral
  study, because in an oral study the anaphylaxis rule is not there at all.

What is NEVER merged is a difference in ACTION. "This subject receives no
further study drug" and "no further subject is dosed" are the two decisions the
whole module exists to keep apart; two rules that share a trigger but act at
different levels are not duplicates, they are the pair. Nor is a numeric
threshold ever lost to a merge: the surviving rule carries every number both
rules carried.

DESIGN AWARENESS
----------------
In a crossover each subject is dosed more than once, so "withdraw from further
dosing" has real content: the subject is not dosed in any subsequent period. In
a parallel study dosing is complete after the single administration, so the same
rule can only be about treatment, observation and follow-up - claiming a later
dose will be withheld would be meaningless text.

REGULATORY FRAMING
------------------
ICH E6(R2) 4.12 / 6.4.7 / 6.7; CTCAE v5.0 (NCI, 27-Nov-2017); ICH E2A and
21 CFR 312.32 (seriousness and expedited reporting); FDA "Drug-Induced Liver
Injury: Premarketing Clinical Evaluation" (July 2009) for Hy's Law; ICH E14 and
its Q&A for the QTc boundaries; EMA first-in-human/early-phase guideline
EMEA/CHMP/SWP/28367/07 Rev 1 for sentinel dosing; Declaration of Helsinki for
the healthy-volunteer framing (no prospect of therapeutic benefit, so tolerance
for related severe events is essentially zero).

SCOPE
-----
This is protocol CONTENT, not study conduct. The module emits the pre-specified
rules, thresholds and discharge criteria that belong in the protocol package. It
does not implement AE intake, causality adjudication, an unblinding workflow, or
any state machine that tracks whether a halt is currently in force.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------
# Every number the engine emits is either (a) a published convention, cited on
# the line where it is defined, or (b) an ASSUMPTION - a unit-practice default
# that no guideline fixes. Assumptions are surfaced in the result under
# "assumptions" so the protocol can disclose them and a reviewer can change
# them, rather than having them silently baked into narrative text.

# -- Cohort halting arithmetic ---------------------------------------------
# ASSUMPTION. No guideline supplies a numeric halting fraction for a
# healthy-volunteer BE/PK study; conventional Phase I practice is "a couple of
# related severe events is already too many". A fraction alone breaks at both
# ends, so it is bounded: the FLOOR stops a 6-subject pilot from halting on one
# idiosyncratic event, and the CAP stops a 200-subject study from arithmetically
# tolerating 20 severe related events.
_HALT_RELATED_G3_FRACTION = 0.10
_HALT_RELATED_G3_FLOOR = 2
_HALT_RELATED_G3_CAP = 5

# ASSUMPTION. Clustering of the SAME moderate related event is the signature of
# a product-quality, batch or dosing-procedure problem rather than background
# morbidity, so it gets its own (higher) count.
_HALT_CLUSTER_G2_FRACTION = 0.25
_HALT_CLUSTER_G2_FLOOR = 3
_HALT_CLUSTER_G2_CAP = 8

# ASSUMPTION. AE-driven withdrawals at or beyond the dropout allowance the study
# was powered for mean the study can no longer deliver its evaluable N, so
# further dosing exposes subjects to risk the study can no longer convert into
# an answer. Overridden by the real dropout allowance when it is supplied.
_HALT_AE_WITHDRAWAL_FRACTION = 0.20
_HALT_AE_WITHDRAWAL_FLOOR = 3

# ASSUMPTION. One QTc outlier can be an artefact (lead placement, measurement,
# electrolytes); two is a drug effect.
_HALT_QT_SUBJECTS = 2

# ASSUMPTION. A patient population carries background disease morbidity, so a
# count built for healthy volunteers would halt on events the disease produces
# anyway. The counted events are already restricted to RELATED events; the
# uplift is a second, deliberate margin.
_PATIENT_HALT_COUNT_UPLIFT = 1

# For a narrow-therapeutic-index drug a single exposure outlier can be harmful,
# so the cohort tally is reduced to one related severe event.
_NTI_G3_HALT_COUNT = 1

# -- Hepatotoxicity (FDA DILI guidance, July 2009) --------------------------
_HYS_LAW_AMINOTRANSFERASE_ULN_X = 3.0   # ALT or AST
_HYS_LAW_BILIRUBIN_ULN_X = 2.0          # total bilirubin
_HYS_LAW_ALP_MAX_ULN_X = 2.0            # ALP must stay below this (excludes cholestasis)
_ALT_DISCONTINUE_ULN_X = 8.0            # discontinue outright
_ALT_PERSISTENT_ULN_X = 5.0             # discontinue if sustained
_ALT_PERSISTENT_DAYS = 14
_ALT_SYMPTOMATIC_ULN_X = 3.0            # discontinue if accompanied by symptoms
_EOSINOPHILIA_PCT = 5.0
_INR_HEPATIC_THRESHOLD = 1.5

# -- QT (ICH E14 and ICH E14 Q&A) -------------------------------------------
_QTCF_ABSOLUTE_STOP_MS = 500.0          # absolute value of regulatory concern
_QTCF_DELTA_STOP_MS = 60.0              # change from baseline of regulatory concern
_QTCF_MONITOR_UNTIL_MS = 480.0          # continue monitoring until below this
_QTCF_DELTA_MONITOR_UNTIL_MS = 30.0
_ECG_REPEAT_WINDOW_MIN = 30             # confirm on a repeat triplicate ECG
_PREDOSE_POTASSIUM_MIN_MMOL_L = 3.5     # correct before dosing a QT-risk drug
_PREDOSE_MAGNESIUM_MIN_MMOL_L = 0.7

# -- Vital-sign stopping boundaries (individual) ----------------------------
# ASSUMPTION. Conventional Phase I unit action limits; confirmed on a repeat
# after supine rest so a single artefactual reading cannot stop a subject.
_STOP_SBP_MMHG = (85, 160)
_STOP_DBP_MMHG = (45, 100)
_STOP_PULSE_BPM = (40, 120)
_STOP_SPO2_PCT = 92
_STOP_TEMP_C = 39.0
_VITALS_REST_MIN = 5

# -- Confinement discharge criteria -----------------------------------------
# ASSUMPTION. Conventional Phase I unit discharge ranges. These are narrower
# than the stopping boundaries above by design: a subject is stopped when
# something is wrong, but is only discharged when everything is right.
_DISCHARGE_SBP_MMHG = (90, 140)
_DISCHARGE_DBP_MMHG = (50, 90)
_DISCHARGE_PULSE_BPM = (45, 100)
_DISCHARGE_RR_PER_MIN = (12, 20)
_DISCHARGE_SPO2_MIN_PCT = 95
_DISCHARGE_TEMP_MAX_C = 37.8
_DISCHARGE_QTCF_MAX_MS = 480.0
_DISCHARGE_ORTHOSTATIC_SBP_DROP_MMHG = 20   # standard orthostatic hypotension definition
_DISCHARGE_ORTHOSTATIC_DBP_DROP_MMHG = 10
_DISCHARGE_VITALS_CONFIRM_INTERVAL_MIN = 30
# ASSUMPTION. Minimum in-unit observation after dosing before discharge can even
# be considered; the binding constraint in practice is the last in-unit PK
# sample, which is usually later than this.
_MIN_POST_DOSE_OBSERVATION_HOURS = 24.0
# ASSUMPTION. Anaphylaxis after a parenteral biologic is overwhelmingly an
# early event (minutes to ~2 h), so direct observation with resuscitation
# capability is held for 4 h after injection.
_INJECTION_DIRECT_OBSERVATION_HOURS = 4.0

# -- Sentinel dosing (EMA EMEA/CHMP/SWP/28367/07 Rev 1) ---------------------
_SENTINEL_COHORT_N = 2
_SENTINEL_OBSERVATION_HOURS = 24.0      # ASSUMPTION: conventional interval

# -- Oral BE emesis exclusion (FDA BE guidance) -----------------------------
_EMESIS_TMAX_MULTIPLE = 2.0

# -- Risk-topic thresholds --------------------------------------------------
_HYPOGLYCEMIA_L2_MG_DL = 54.0           # ADA Level 2 (clinically significant), = 3.0 mmol/L
_HYPOGLYCEMIA_L1_MG_DL = 70.0           # ADA Level 1 (alert value), = 3.9 mmol/L
_ANC_STOP_X10E9_L = 1.0                 # CTCAE v5.0 Grade 3 neutropenia
_PLATELET_STOP_X10E9_L = 50.0           # CTCAE v5.0 Grade 3 thrombocytopenia
_HB_STOP_G_DL = 8.0                     # CTCAE v5.0 Grade 3 anaemia
_HB_DROP_G_DL = 2.0                     # bleeding signal
_RESP_RATE_STOP_PER_MIN = 8
_RESP_SPO2_STOP_PCT = 90
_CREATININE_AKI_MULTIPLE = 1.5          # KDIGO 2012 AKI stage 1
_CREATININE_AKI_RISE_MG_DL = 0.3
_CREATININE_AKI_WINDOW_HOURS = 48
_LACTATE_STOP_MMOL_L = 5.0
_ARTERIAL_PH_STOP = 7.35
_INR_BLEEDING_THRESHOLD = 4.0


# ---------------------------------------------------------------------------
# AE severity grading - structured, not prose
# ---------------------------------------------------------------------------
# CTCAE v5.0 (US DHHS / NCI, published 27-Nov-2017). The generic grade
# definitions are reproduced because the protocol must state them; the
# protocol_action column is THIS study's pre-specified consequence, which CTCAE
# itself does not supply.


@dataclass(frozen=True)
class AeGrade:
    grade: int
    title: str
    definition: str
    protocol_action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_CTCAE_GRADES: tuple[AeGrade, ...] = (
    AeGrade(
        grade=1,
        title="Mild",
        definition=(
            "Asymptomatic or mild symptoms; clinical or diagnostic observations only; "
            "intervention not indicated."
        ),
        protocol_action=(
            "Record the event and follow it to resolution. Dosing continues; no gating "
            "consequence."
        ),
    ),
    AeGrade(
        grade=2,
        title="Moderate",
        definition=(
            "Minimal, local or non-invasive intervention indicated; limiting "
            "age-appropriate instrumental activities of daily living (preparing meals, "
            "shopping, using the telephone, managing money)."
        ),
        protocol_action=(
            "Record, treat and monitor. A subject with an ONGOING related Grade 2 event "
            "may not be discharged from confinement and may not receive a subsequent "
            "period dose until the event has resolved to Grade <= 1 or the Investigator "
            "has documented that continued dosing is safe. Counts toward the clustering "
            "halting rule."
        ),
    ),
    AeGrade(
        grade=3,
        title="Severe",
        definition=(
            "Severe or medically significant but not immediately life-threatening; "
            "hospitalisation or prolongation of hospitalisation indicated; disabling; "
            "limiting self-care activities of daily living (bathing, dressing, feeding, "
            "toileting)."
        ),
        protocol_action=(
            "If related: the subject receives no further study drug, and the event counts "
            "toward the cohort halting tally. If unrelated: Investigator decision, "
            "documented with the alternative cause."
        ),
    ),
    AeGrade(
        grade=4,
        title="Life-threatening",
        definition="Life-threatening consequences; urgent intervention indicated.",
        protocol_action=(
            "Immediate permanent discontinuation for that subject AND immediate halt of "
            "dosing for all further subjects, at any sample size - this rule is never "
            "scaled by N."
        ),
    ),
    AeGrade(
        grade=5,
        title="Death",
        definition="Death related to the adverse event.",
        protocol_action=(
            "Immediate halt of all dosing, notification of the Sponsor within 24 hours, "
            "expedited reporting per 21 CFR 312.32, and IRB/IEC notification. Dosing does "
            "not resume without a protocol amendment."
        ),
    ),
)

# The single most commonly muddled point in a safety section, stated once and
# carried into the prompt verbatim.
_SEVERITY_VS_SERIOUSNESS = (
    "Severity and seriousness are independent axes. Severity (CTCAE Grade 1-5) measures "
    "the INTENSITY of an event; seriousness is a regulatory outcome classification "
    "(death, life-threatening, hospitalisation or its prolongation, persistent or "
    "significant disability/incapacity, congenital anomaly, or an important medical event "
    "requiring intervention to prevent one of those). A Grade 1 event can be serious (for "
    "example a brief hospitalisation for observation) and a Grade 3 event can be "
    "non-serious. The gating rules below name the axis they act on explicitly."
)

# "Related" is the axis most of the counted rules turn on, so the definition is
# locked rather than left to the model.
_CAUSALITY_BASIS = (
    "An event is 'related' when the Investigator judges there is a reasonable possibility "
    "that the study drug caused it (ICH E2A binary attribution). Where the Investigator "
    "assesses an event as not related, the alternative cause is documented in the source "
    "record. Events with a missing or pending causality assessment are counted AS RELATED "
    "for the purpose of every halting rule until the assessment is documented, so that an "
    "unfinished assessment can never postpone a halt."
)


# ---------------------------------------------------------------------------
# Rule catalogue
# ---------------------------------------------------------------------------
# tier ranks the urgency of the response:
#   1 = immediate, no discretion (stop / halt the moment the trigger is met)
#   2 = stop or halt pending a documented medical review
#   3 = enhanced monitoring or a pre-dose gate, not itself a stop
#
# scope is the load-bearing field: "individual" acts on one subject, "cohort"
# acts on the trial. Nothing is allowed to be both - where the same clinical
# event drives both levels (Hy's Law, anaphylaxis, Grade 4/5) there are TWO
# catalogue entries with distinct keys, so a reader can never mistake a subject
# withdrawal for a study halt.
#
# trigger/action/label may contain the substitution placeholders k_g3, k_qt,
# k_cluster, k_withdraw, n, n_phrase and redose_clause, which are filled per
# study. Literal braces must not appear in any of these strings.


@dataclass(frozen=True)
class SafetyRule:
    key: str
    scope: str                 # "individual" | "cohort"
    tier: int
    label: str
    trigger: str               # the countable boundary - applied without judgement
    action: str                # what happens the moment the trigger is met
    rationale: str             # why this boundary and not another
    evidence: str              # the guideline / convention it rests on
    applies_when: str
    thresholds: dict[str, Any] = field(default_factory=dict)
    count_key: str = ""        # which per-study count this rule is scaled by
    added_because: str = ""    # populated when a label cue or escalation added it

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_CORE_INDIVIDUAL: tuple[SafetyRule, ...] = (
    # MERGED RULE. This was two catalogue entries - "Life-threatening or fatal
    # adverse event" (ind_grade4_or_5) and "Serious adverse event"
    # (ind_sae) - until a reviewing pharmacologist pointed out that they are one
    # rule written twice: CTCAE Grade 4 IS "life-threatening" and Grade 5 IS
    # "death", which are two of the six ICH E2A seriousness criteria, and both
    # entries carried the identical action (this subject receives no further
    # study drug). Both limbs of the trigger are named below, so nothing a
    # monitor could act on was lost; only the second row was.
    SafetyRule(
        key="ind_sae",
        scope="individual",
        tier=1,
        label="Serious, life-threatening or fatal adverse event",
        trigger=(
            "The subject experiences any CTCAE v5.0 Grade 4 (life-threatening) or Grade 5 "
            "(fatal) adverse event, or any other event meeting an ICH E2A seriousness "
            "criterion - death, life-threatening, inpatient hospitalisation or its "
            "prolongation, persistent or significant disability/incapacity, congenital "
            "anomaly, or an important medical event requiring intervention to prevent one "
            "of these - irrespective of causality assessment."
        ),
        action=(
            "Immediate permanent discontinuation of study drug for this subject"
            "{redose_clause}, with immediate Investigator assessment and emergency "
            "management per the unit's standard operating procedures. Notify the Sponsor "
            "within 24 hours, report on an expedited basis per 21 CFR 312.32, and follow "
            "the event until resolution, stabilisation, or a documented stable chronic "
            "outcome. Where the event is assessed as related, the single-event cohort "
            "halting rule applies in addition."
        ),
        rationale=(
            "Severity and seriousness are independent axes, but Grade 4 and Grade 5 sit "
            "inside the seriousness definition, so listing them as a separate withdrawal "
            "rule gave a monitor two rows to reconcile for one decision. At either "
            "boundary the subject is not re-exposed while the event is characterised, and "
            "in a study with no prospect of therapeutic benefit there is no counterweight "
            "that could justify a further dose."
        ),
        evidence="ICH E2A; 21 CFR 312.32; ICH E6(R2) 6.4.7; CTCAE v5.0 grade definitions",
        applies_when="Always",
    ),
    SafetyRule(
        key="ind_related_grade3",
        scope="individual",
        tier=1,
        label="Treatment-related severe (Grade >= 3) adverse event",
        trigger=(
            "The subject experiences any adverse event assessed as related to study drug "
            "at CTCAE v5.0 Grade 3 or higher."
        ),
        action=(
            "The subject receives no further study drug{redose_clause}. The Investigator "
            "assesses the subject, treatment is given as clinically indicated, and the "
            "event is followed to resolution or stabilisation. All scheduled SAFETY "
            "assessments continue; PK handling of the incomplete profile follows the "
            "statistical analysis plan. The event is added to the cumulative cohort tally."
        ),
        rationale=(
            "A severe event that the Investigator judges drug-related removes any basis for "
            "re-exposing that individual, but a single such event in a study of this size "
            "is not by itself proof of an unacceptable population-level risk - that is what "
            "the separate, counted cohort rule is for."
        ),
        evidence="ICH E6(R2) 6.4.7 subject withdrawal criteria; CTCAE v5.0",
        applies_when="Always",
    ),
    SafetyRule(
        key="ind_hys_law",
        scope="individual",
        tier=1,
        label="Hy's Law case - hepatocellular injury with impaired bilirubin clearance",
        trigger=(
            "ALT OR AST >= 3 x the upper limit of normal AND total bilirubin >= 2 x the "
            "upper limit of normal AND alkaline phosphatase < 2 x the upper limit of "
            "normal, with no alternative explanation for the combination (viral hepatitis "
            "A/B/C/E, EBV or CMV infection, alcohol, hypoxic or ischaemic hepatopathy, "
            "biliary obstruction, autoimmune hepatitis, or another hepatotoxic agent "
            "including paracetamol/acetaminophen or a herbal product)."
        ),
        action=(
            "Permanent discontinuation of study drug for this subject; the subject is not "
            "discharged from the unit on the basis of an unresolved finding. Repeat liver "
            "chemistry within 24-48 hours and then 2-3 times weekly until values are "
            "falling and the abnormality has resolved or stabilised. Obtain a "
            "hepatology/gastroenterology assessment and complete the alternative-cause "
            "workup. Report as a serious adverse event (an important medical event). This "
            "case also independently triggers the cohort halting rule."
        ),
        rationale=(
            "The combination of hepatocellular injury with a bilirubin rise, once other "
            "causes are excluded, indicates loss of hepatic reserve rather than adaptation. "
            "Historically it predicts a rate of severe drug-induced liver injury on the "
            "order of one in ten similarly affected subjects, which is why the alkaline "
            "phosphatase constraint (excluding cholestatic patterns) and the "
            "alternative-cause requirement are part of the definition rather than "
            "afterthoughts."
        ),
        evidence=(
            "FDA Guidance for Industry, Drug-Induced Liver Injury: Premarketing Clinical "
            "Evaluation (July 2009)"
        ),
        applies_when="Always - liver chemistry is collected in every study of this type",
        thresholds={
            "alt_or_ast_uln_multiple": _HYS_LAW_AMINOTRANSFERASE_ULN_X,
            "total_bilirubin_uln_multiple": _HYS_LAW_BILIRUBIN_ULN_X,
            "alkaline_phosphatase_max_uln_multiple": _HYS_LAW_ALP_MAX_ULN_X,
            "alternative_cause_excluded_required": True,
        },
    ),
    SafetyRule(
        key="ind_hepatic_discontinuation",
        scope="individual",
        tier=2,
        label="Liver chemistry discontinuation thresholds (non-Hy's-Law)",
        trigger=(
            "Any one of the following, confirmed on a repeat sample: ALT or AST > 8 x the "
            "upper limit of normal; ALT or AST > 5 x the upper limit of normal persisting "
            "for more than 14 days; ALT or AST > 3 x the upper limit of normal accompanied "
            "by fatigue, nausea, vomiting, right upper quadrant pain or tenderness, fever, "
            "rash or eosinophilia > 5 percent; or ALT or AST > 3 x the upper limit of "
            "normal with INR > 1.5."
        ),
        action=(
            "Discontinue study drug for this subject{redose_clause} and follow liver "
            "chemistry until resolution. Confinement discharge requires the values to be "
            "falling and the Investigator to document that outpatient follow-up is safe."
        ),
        rationale=(
            "These are the FDA DILI discontinuation thresholds for aminotransferase "
            "elevation that has not (yet) produced a bilirubin rise. They catch injury "
            "early, before the Hy's Law combination assembles."
        ),
        evidence="FDA DILI Premarketing Clinical Evaluation guidance (July 2009)",
        applies_when="Always",
        thresholds={
            "alt_ast_discontinue_uln_multiple": _ALT_DISCONTINUE_ULN_X,
            "alt_ast_persistent_uln_multiple": _ALT_PERSISTENT_ULN_X,
            "persistence_days": _ALT_PERSISTENT_DAYS,
            "alt_ast_symptomatic_uln_multiple": _ALT_SYMPTOMATIC_ULN_X,
            "eosinophilia_pct": _EOSINOPHILIA_PCT,
            "inr": _INR_HEPATIC_THRESHOLD,
        },
    ),
    SafetyRule(
        key="ind_qt_prolongation",
        scope="individual",
        tier=1,
        label="QT prolongation",
        trigger=(
            "QTcF > 500 ms, OR an increase in QTcF of more than 60 ms from the pre-dose "
            "baseline, on a triplicate 12-lead ECG confirmed by a repeat triplicate within "
            "30 minutes (the mean of the triplicate is the value used)."
        ),
        action=(
            "No further study drug for this subject{redose_clause}. Place the subject on "
            "continuous ECG monitoring, measure and correct serum potassium and magnesium, "
            "withhold any QT-prolonging concomitant medication, and obtain a cardiology "
            "review. The subject is NOT discharged until QTcF is below 480 ms and within "
            "30 ms of baseline. {k_qt} or more subjects meeting this boundary independently "
            "trigger the cohort halting rule."
        ),
        rationale=(
            "500 ms absolute and a 60 ms increase from baseline are the two thresholds ICH "
            "E14 identifies as being of regulatory concern for proarrhythmic risk; the "
            "confirmatory repeat exists because lead placement, posture and a single "
            "automated read are common sources of a spurious value."
        ),
        evidence="ICH E14 and ICH E14 Questions and Answers",
        applies_when="Always - 12-lead ECGs are collected in every study of this type",
        thresholds={
            "qtcf_absolute_ms": _QTCF_ABSOLUTE_STOP_MS,
            "qtcf_delta_from_baseline_ms": _QTCF_DELTA_STOP_MS,
            "monitor_until_qtcf_below_ms": _QTCF_MONITOR_UNTIL_MS,
            "monitor_until_delta_below_ms": _QTCF_DELTA_MONITOR_UNTIL_MS,
            "confirmatory_repeat_minutes": _ECG_REPEAT_WINDOW_MIN,
        },
    ),
    SafetyRule(
        key="ind_vital_signs",
        scope="individual",
        tier=2,
        label="Vital-sign action limits",
        trigger=(
            "Any of the following, confirmed on a repeat measurement after at least 5 "
            "minutes supine rest: systolic blood pressure below 85 or above 160 mmHg; "
            "diastolic blood pressure below 45 or above 100 mmHg; pulse below 40 or above "
            "120 beats per minute; oxygen saturation below 92 percent on room air; or "
            "body temperature at or above 39.0 degrees Celsius."
        ),
        action=(
            "Withhold further study drug pending Investigator review{redose_clause}, treat "
            "symptomatically, and continue monitoring at intervals no longer than 30 "
            "minutes until values return to the discharge ranges. The subject is not "
            "discharged while any value remains outside the discharge ranges."
        ),
        rationale=(
            "Haemodynamic and respiratory limits give the unit an objective, "
            "immediately-measurable trigger that does not wait for an event to be graded "
            "and coded; the confirmatory repeat prevents a single artefactual cuff reading "
            "from removing a subject from the study."
        ),
        evidence="Conventional Phase I unit action limits (ASSUMPTION - see assumptions)",
        applies_when="Always",
        thresholds={
            "systolic_mmHg": list(_STOP_SBP_MMHG),
            "diastolic_mmHg": list(_STOP_DBP_MMHG),
            "pulse_bpm": list(_STOP_PULSE_BPM),
            "spo2_min_pct": _STOP_SPO2_PCT,
            "temperature_max_c": _STOP_TEMP_C,
            "supine_rest_minutes": _VITALS_REST_MIN,
        },
    ),
    SafetyRule(
        key="ind_pregnancy",
        scope="individual",
        tier=2,
        label="Confirmed pregnancy",
        trigger=(
            "A positive pregnancy test in a subject of childbearing potential at any point "
            "after the pre-dose test, or a reported pregnancy in a partner where the "
            "protocol collects that information."
        ),
        action=(
            "No further study drug for this subject{redose_clause}; the subject is "
            "withdrawn from further dosing and followed per the pregnancy reporting "
            "procedure. This is a withdrawal criterion, not an adverse event in itself."
        ),
        rationale=(
            "Foetal exposure in a study with no prospect of benefit to the subject is not "
            "acceptable, and the gating consequence must be stated separately from the "
            "reporting obligation so the unit knows dosing stops immediately."
        ),
        evidence="ICH E6(R2) 6.4.7; standard exclusion/withdrawal practice",
        applies_when="Subjects of childbearing potential are enrolled",
    ),
    SafetyRule(
        key="ind_investigator_judgement",
        scope="individual",
        tier=3,
        label="Investigator judgement (documented)",
        trigger=(
            "Any clinical finding that, in the Investigator's documented judgement, makes "
            "further dosing of this subject unsafe, including a subject who withdraws "
            "consent to continue dosing."
        ),
        action=(
            "No further study drug for this subject{redose_clause}; the reason is recorded "
            "in the source document and on the CRF."
        ),
        rationale=(
            "Explicitly bounded catch-all. It is listed LAST and marked as the only "
            "judgement-based individual rule so that the preceding rules remain countable "
            "boundaries a monitor can apply without interpretation."
        ),
        evidence="ICH E6(R2) 6.4.7",
        applies_when="Always",
    ),
)


_CORE_COHORT: tuple[SafetyRule, ...] = (
    SafetyRule(
        key="coh_grade4_or_5",
        scope="cohort",
        tier=1,
        label="Any single life-threatening or fatal related event",
        trigger=(
            "ANY single adverse event assessed as related to study drug at CTCAE v5.0 "
            "Grade 4 (life-threatening) or Grade 5 (fatal), irrespective of how many "
            "subjects have been dosed. This rule is NOT scaled by sample size."
        ),
        action=(
            "Halt dosing of all further subjects immediately. Subjects already dosed "
            "continue safety follow-up and, in a multi-period design, receive no further "
            "period doses. Notify the Sponsor within 24 hours and the IRB/IEC per its "
            "reporting requirements. Dosing does not resume without a documented joint "
            "written review by the Sponsor Medical Monitor and the Principal Investigator "
            "and, where the resumption changes dose, population or monitoring, a protocol "
            "amendment."
        ),
        rationale=(
            "Scaling this rule by N would be an arithmetic accident with a clinical "
            "consequence: a 10 percent rule at n=60 would tolerate several life-threatening "
            "events before halting. In a study whose subjects receive no therapeutic "
            "benefit, one is already the answer."
        ),
        evidence="ICH E6(R2) 4.12 premature termination or suspension; Declaration of Helsinki",
        applies_when="Always",
    ),
    SafetyRule(
        key="coh_related_sae",
        scope="cohort",
        tier=1,
        label="Any single related serious adverse event",
        trigger=(
            "ANY single serious adverse event (ICH E2A criteria) assessed by the "
            "Investigator or the Sponsor as having a reasonable possibility of being "
            "related to study drug, irrespective of the number of subjects dosed."
        ),
        action=(
            "Halt dosing of all further subjects pending a documented joint Sponsor "
            "Medical Monitor and Principal Investigator review, with IRB/IEC notification. "
            "Record the halt, its trigger, and the resumption decision with dates in the "
            "trial master file."
        ),
        rationale=(
            "A related SAE in a healthy-volunteer or PK-similarity population is a signal "
            "about the product, not about one subject's background morbidity, so exposure "
            "of further subjects pauses while it is characterised."
        ),
        evidence="ICH E6(R2) 4.12; 21 CFR 312.32",
        applies_when="Always",
    ),
    SafetyRule(
        key="coh_hys_law",
        scope="cohort",
        tier=1,
        label="Any single confirmed Hy's Law case",
        trigger=(
            "ANY single subject meets the Hy's Law criteria (ALT or AST >= 3 x upper limit "
            "of normal with total bilirubin >= 2 x upper limit of normal, alkaline "
            "phosphatase < 2 x upper limit of normal, and no alternative explanation), "
            "irrespective of the number of subjects dosed."
        ),
        action=(
            "Halt dosing of all further subjects immediately, complete the alternative-cause "
            "workup, and do not resume without documented Sponsor and Investigator review "
            "and IRB/IEC notification."
        ),
        rationale=(
            "One confirmed Hy's Law case among a few dozen exposures implies a severe "
            "hepatotoxicity rate that no study without therapeutic benefit can justify "
            "continuing to sample."
        ),
        evidence="FDA DILI Premarketing Clinical Evaluation guidance (July 2009)",
        applies_when="Always",
        thresholds={
            "subject_count": 1,
            "alt_or_ast_uln_multiple": _HYS_LAW_AMINOTRANSFERASE_ULN_X,
            "total_bilirubin_uln_multiple": _HYS_LAW_BILIRUBIN_ULN_X,
        },
    ),
    SafetyRule(
        key="coh_related_grade3_count",
        scope="cohort",
        tier=1,
        label="Cumulative related severe (Grade >= 3) events",
        trigger=(
            "{k_g3} or more subjects {n_phrase} experience an adverse event of any type "
            "assessed as related to study drug at CTCAE v5.0 Grade 3 or higher. The count "
            "is cumulative across the whole study, not per period or per cohort."
        ),
        action=(
            "Halt dosing of any further subjects. Subjects already dosed complete safety "
            "follow-up and receive no further period doses. Dosing resumes only after a "
            "documented joint written review by the Sponsor Medical Monitor and the "
            "Principal Investigator, with IRB/IEC notification; any resumption that changes "
            "the dose, the population, or the monitoring plan requires a protocol amendment."
        ),
        rationale=(
            "This is the rule that converts individual withdrawals into a study-level "
            "decision. The count is scaled to the planned sample size, with a floor so a "
            "small study cannot halt on a single idiosyncratic event and a cap so a large "
            "study cannot arithmetically tolerate an unreasonable absolute number."
        ),
        evidence="ICH E6(R2) 4.12; count is an ASSUMPTION - see assumptions",
        applies_when="Always",
        count_key="k_g3",
    ),
    SafetyRule(
        key="coh_qt_count",
        scope="cohort",
        tier=1,
        label="Cumulative QT stopping-boundary subjects",
        trigger=(
            "{k_qt} or more subjects meet the individual QT stopping boundary (confirmed "
            "QTcF > 500 ms, or a confirmed increase of more than 60 ms from baseline)."
        ),
        action=(
            "Halt dosing of any further subjects pending cardiology and Sponsor review of "
            "all ECG data, including central over-read of the affected tracings."
        ),
        rationale=(
            "A single QTc outlier may be measurement or electrolyte artefact; a second in "
            "the same study is a drug effect until proven otherwise, and proarrhythmic risk "
            "is not something to sample further in unpaid, unbenefited subjects."
        ),
        evidence="ICH E14; subject count is an ASSUMPTION - see assumptions",
        applies_when="Always",
        count_key="k_qt",
    ),
    SafetyRule(
        key="coh_cluster_grade2",
        scope="cohort",
        tier=2,
        label="Clustering of the same moderate related event",
        trigger=(
            "{k_cluster} or more subjects {n_phrase} report the SAME related adverse event "
            "(identical MedDRA Preferred Term) at CTCAE v5.0 Grade 2 or higher."
        ),
        action=(
            "Halt dosing of any further subjects pending a documented review that "
            "specifically examines investigational and comparator batch records, "
            "preparation and administration procedure, and dosing conditions, in addition "
            "to the clinical picture."
        ),
        rationale=(
            "Repetition of one specific moderate event is the signature of a product "
            "quality, batch or procedural problem rather than random background morbidity, "
            "and it will not be caught by a severity-based rule because no single event "
            "reaches Grade 3."
        ),
        evidence="ICH E6(R2) 4.12; count is an ASSUMPTION - see assumptions",
        applies_when="Always",
        count_key="k_cluster",
    ),
    SafetyRule(
        key="coh_ae_withdrawals",
        scope="cohort",
        tier=2,
        label="Cumulative adverse-event withdrawals",
        trigger=(
            "{k_withdraw} or more dosed subjects {n_phrase} are withdrawn from dosing for "
            "adverse-event reasons."
        ),
        action=(
            "Halt dosing of any further subjects pending a joint Sponsor and Investigator "
            "review of both the safety picture and the study's ability to reach its "
            "evaluable sample size."
        ),
        rationale=(
            "Two problems converge at this boundary: a tolerability signal, and the fact "
            "that AE withdrawals at or beyond the dropout allowance the study was powered "
            "for mean the study can no longer produce an interpretable answer - so further "
            "exposure carries risk that cannot be converted into evidence."
        ),
        evidence="ICH E6(R2) 4.12; count is tied to the planned dropout allowance",
        applies_when="Always",
        count_key="k_withdraw",
    ),
    SafetyRule(
        key="coh_medical_monitor_judgement",
        scope="cohort",
        tier=3,
        label="Sponsor or Investigator judgement (documented)",
        trigger=(
            "Any safety finding that, in the documented judgement of the Principal "
            "Investigator or the Sponsor Medical Monitor, raises a concern for subject "
            "safety that the rules above do not anticipate."
        ),
        action=(
            "Halt dosing of any further subjects and document the finding, the reasoning "
            "and the resumption decision."
        ),
        rationale=(
            "Explicitly bounded catch-all. It is listed LAST and marked as the only "
            "judgement-based halting rule, so that every other halting rule in this "
            "protocol is a count a monitor can apply without interpretation."
        ),
        evidence="ICH E6(R2) 4.12",
        applies_when="Always",
    ),
)


# Parenteral / biologic gating. A subcutaneous or intravenous biological product
# carries two hazards an oral small-molecule BE study does not: systemic
# hypersensitivity (including anaphylaxis, which is minutes-to-hours in onset)
# and local injection-site reactions.
_INJECTABLE_RULES: tuple[SafetyRule, ...] = (
    SafetyRule(
        key="ind_anaphylaxis",
        scope="individual",
        tier=1,
        label="Anaphylaxis or systemic hypersensitivity reaction",
        trigger=(
            "Any reaction meeting the NIAID/FAAN clinical criteria for anaphylaxis, or any "
            "systemic hypersensitivity or infusion-related reaction at CTCAE v5.0 Grade 2 "
            "or higher (for example generalised urticaria, angioedema, bronchospasm, "
            "hypotension, or stridor)."
        ),
        action=(
            "Stop or do not start the injection or infusion, treat immediately per the "
            "unit's anaphylaxis standard operating procedure including intramuscular "
            "epinephrine (adult dose 0.3-0.5 mg of 1 mg/mL, repeated as required), and "
            "permanently discontinue study drug for this subject{redose_clause}. Retain a "
            "sample for anti-drug antibody testing. Any anaphylaxis independently triggers "
            "the cohort halting rule."
        ),
        rationale=(
            "Onset is typically within minutes to two hours of parenteral exposure and can "
            "be fatal, so the trigger has to be recognisable at the bedside and the "
            "response has to be pre-authorised rather than decided in the moment."
        ),
        evidence="NIAID/FAAN 2006 clinical criteria for anaphylaxis; CTCAE v5.0",
        applies_when="A parenteral (subcutaneous, intramuscular or intravenous) product is administered",
    ),
    SafetyRule(
        key="coh_anaphylaxis",
        scope="cohort",
        tier=1,
        label="Any single case of anaphylaxis",
        trigger=(
            "ANY single subject experiences anaphylaxis meeting the NIAID/FAAN criteria, "
            "irrespective of the number of subjects dosed."
        ),
        action=(
            "Halt dosing of all further subjects immediately; do not resume without "
            "documented Sponsor and Investigator review, IRB/IEC notification and, where "
            "the product handling or premedication changes, a protocol amendment."
        ),
        rationale=(
            "Anaphylaxis to a biological product is a product-level signal and the next "
            "subject's reaction cannot be predicted from the first one's severity."
        ),
        evidence="ICH E6(R2) 4.12; NIAID/FAAN criteria",
        applies_when="A parenteral product is administered",
        thresholds={"subject_count": 1},
    ),
    SafetyRule(
        key="ind_injection_site_reaction",
        scope="individual",
        tier=2,
        label="Injection-site reaction",
        trigger=(
            "A CTCAE v5.0 Grade 3 or higher injection-site reaction, or a Grade 2 "
            "injection-site reaction that has not resolved to Grade <= 1 within 72 hours."
        ),
        action=(
            "No further injections for this subject{redose_clause}. Measure and record "
            "erythema and induration in two dimensions, photograph where the site permits "
            "it and the subject consents, and follow to resolution."
        ),
        rationale=(
            "Local tolerability is a comparative endpoint in a biosimilar PK study as well "
            "as a safety matter; an objective size-and-duration trigger keeps it from being "
            "graded by impression."
        ),
        evidence="CTCAE v5.0 injection-site reaction grading",
        applies_when="A subcutaneous or intramuscular product is administered",
    ),
    SafetyRule(
        key="coh_injection_site_cluster",
        scope="cohort",
        tier=2,
        label="Clustering of injection-site reactions",
        trigger=(
            "{k_cluster} or more subjects {n_phrase} experience a CTCAE v5.0 Grade 2 or "
            "higher injection-site reaction."
        ),
        action=(
            "Halt dosing of any further subjects pending review of injection technique, "
            "injection site, needle and device handling, product temperature and batch "
            "records."
        ),
        rationale=(
            "Clustered local reactions usually indicate technique, device or batch rather "
            "than the molecule, and that is a fixable cause worth stopping to find."
        ),
        evidence="ICH E6(R2) 4.12; count is an ASSUMPTION - see assumptions",
        applies_when="A subcutaneous or intramuscular product is administered",
        count_key="k_cluster",
    ),
)


# Oral gating that has no parenteral analogue.
_ORAL_RULES: tuple[SafetyRule, ...] = (
    SafetyRule(
        key="ind_emesis",
        scope="individual",
        tier=3,
        label="Emesis within the absorption window",
        trigger=(
            "The subject vomits at or before 2 times the median Tmax of the reference "
            "product for an immediate-release formulation, or at any time within the "
            "labelled dosing interval for a modified-release formulation."
        ),
        action=(
            "The subject completes all scheduled safety and pharmacokinetic assessments, "
            "but the affected period's pharmacokinetic data are excluded from the "
            "bioequivalence analysis per the statistical analysis plan. The event, its "
            "clock time relative to dosing, and the exclusion decision are documented "
            "BEFORE database lock. This is a data-handling gate, not a safety withdrawal: "
            "a subject who vomits is withdrawn only if a separate stopping rule is met."
        ),
        rationale=(
            "Vomiting inside the absorption window invalidates the exposure comparison "
            "rather than the subject. Stating the exclusion in advance is what stops it "
            "from looking like post hoc data selection at review."
        ),
        evidence="FDA bioequivalence guidance (subject exclusion for vomiting)",
        applies_when="An oral product is administered",
        thresholds={"median_tmax_multiple": _EMESIS_TMAX_MULTIPLE},
    ),
)


# Escalation rules that are switched on by study context rather than by the
# label text.
_ESCALATION_RULES: tuple[SafetyRule, ...] = (
    SafetyRule(
        key="coh_sentinel_dosing",
        scope="cohort",
        tier=2,
        label="Sentinel dosing of the first subjects",
        trigger=(
            "The first 2 subjects in the first dosing period are dosed as a sentinel group; "
            "no further subject is dosed until at least 24 hours of safety data (adverse "
            "events, vital signs, ECG and available laboratory results) from both sentinel "
            "subjects have been reviewed and the review has been documented."
        ),
        action=(
            "Dosing of the remaining subjects proceeds only after that documented review. "
            "If either sentinel subject meets any individual stopping rule, dosing of "
            "further subjects is halted pending the standard Sponsor and Investigator "
            "review."
        ),
        rationale=(
            "Sentinel dosing converts an unknown-risk first exposure from a group event "
            "into a staged one, so that at most two subjects are exposed before the first "
            "real safety read-out is available."
        ),
        evidence=(
            "EMA guideline on first-in-human and early clinical trials, "
            "EMEA/CHMP/SWP/28367/07 Rev 1; observation interval is an ASSUMPTION"
        ),
        applies_when="Narrow-therapeutic-index drug, a boxed-warning product, or a first parenteral exposure",
        thresholds={
            "sentinel_subjects": _SENTINEL_COHORT_N,
            "observation_hours": _SENTINEL_OBSERVATION_HOURS,
        },
    ),
)


# ---------------------------------------------------------------------------
# Risk-topic rules - switched on by the REAL label text
# ---------------------------------------------------------------------------
# Rather than relying on model recall about which drug is hepatotoxic or
# proarrhythmic, applicability is read off the DailyMed label sections already
# fetched for the protocol. This mirrors services/stratification.py and
# services/design_decisions.py.

_RISK_RULES: dict[str, tuple[SafetyRule, ...]] = {
    "hepatotoxicity": (
        SafetyRule(
            key="ind_hepatic_predose_gate",
            scope="individual",
            tier=3,
            label="Pre-dose liver chemistry gate and enhanced monitoring",
            trigger=(
                "ALT, AST, alkaline phosphatase or total bilirubin above the upper limit of "
                "normal on the pre-dose sample of any dosing period."
            ),
            action=(
                "The subject is not dosed in that period until the value is repeated and is "
                "within normal limits, or the Investigator documents why dosing is safe. "
                "Liver chemistry is measured pre-dose, at 24 and 48 hours after dosing, and "
                "at end of study; any ALT or AST above 3 x the upper limit of normal is "
                "repeated within 24-48 hours and then 2-3 times weekly until it falls below "
                "2 x the upper limit of normal."
            ),
            rationale=(
                "The label identifies hepatic injury as a known risk of this product, so a "
                "baseline abnormality removes the reserve needed to detect and interpret a "
                "drug-related rise, and the standard screening-only schedule would miss the "
                "rise altogether."
            ),
            evidence="Drug label hepatic-injury language; FDA DILI guidance monitoring principles",
            applies_when="The label reports hepatotoxicity or transaminase elevation",
        ),
    ),
    "qt_prolongation": (
        SafetyRule(
            key="ind_qt_predose_gate",
            scope="individual",
            tier=3,
            label="Cardiac pre-dose gate and continuous monitoring",
            trigger=(
                "Pre-dose serum potassium below 3.5 mmol/L, serum magnesium below 0.7 "
                "mmol/L, baseline QTcF at or above 450 ms in males or 460 ms in females, or "
                "any QT-prolonging concomitant medication."
            ),
            action=(
                "Do not dose until the electrolyte abnormality is corrected and confirmed, "
                "the ECG finding is reviewed by the Investigator, and the concomitant "
                "medication has been withheld for at least five of its half-lives. Where the "
                "product's label reports QT prolongation, continuous ECG telemetry runs from "
                "pre-dose until at least 4 hours after dosing or 2 hours past the expected "
                "Tmax, whichever is later, and ECGs are recorded in triplicate."
            ),
            rationale=(
                "Hypokalaemia and hypomagnesaemia are the commonest amplifiers of "
                "drug-induced QT prolongation, and they are cheap to measure and correct. "
                "Dosing a proarrhythmic drug into an uncorrected electrolyte deficit turns "
                "an avoidable risk into an unavoidable one."
            ),
            evidence="ICH E14; drug label QT/torsades language",
            applies_when="The label reports QT prolongation, torsades de pointes or ventricular arrhythmia",
            thresholds={
                "potassium_min_mmol_l": _PREDOSE_POTASSIUM_MIN_MMOL_L,
                "magnesium_min_mmol_l": _PREDOSE_MAGNESIUM_MIN_MMOL_L,
                "baseline_qtcf_max_male_ms": 450.0,
                "baseline_qtcf_max_female_ms": 460.0,
            },
        ),
    ),
    "bleeding": (
        SafetyRule(
            key="ind_bleeding",
            scope="individual",
            tier=2,
            label="Bleeding event",
            trigger=(
                "Any CTCAE v5.0 Grade 2 or higher bleeding event, a fall in haemoglobin of "
                "at least 2 g/dL from baseline, or INR above 4.0 where the product affects "
                "coagulation."
            ),
            action=(
                "No further study drug for this subject{redose_clause}; measure haemoglobin "
                "and coagulation, treat as clinically indicated, and follow to resolution. "
                "A Grade 3 or higher bleeding event is reported as a serious adverse event "
                "and therefore independently triggers the cohort halting rule for a related "
                "serious adverse event."
            ),
            rationale=(
                "The label identifies bleeding as a known risk. Haemoglobin fall and INR are "
                "objective and available on the day, whereas 'clinically significant "
                "bleeding' is not a boundary anyone can apply at 3 a.m."
            ),
            evidence="Drug label haemorrhage/anticoagulation language; CTCAE v5.0",
            applies_when="The label reports haemorrhage, bleeding or anticoagulant activity",
            thresholds={
                "haemoglobin_fall_g_dl": _HB_DROP_G_DL,
                "inr": _INR_BLEEDING_THRESHOLD,
            },
        ),
    ),
    "hypersensitivity": (
        SafetyRule(
            key="ind_hypersensitivity",
            scope="individual",
            tier=1,
            label="Systemic hypersensitivity reaction",
            trigger=(
                "Any systemic hypersensitivity reaction at CTCAE v5.0 Grade 2 or higher, "
                "including generalised urticaria, angioedema, bronchospasm or hypotension."
            ),
            action=(
                "Stop administration where administration is ongoing, treat per the unit's "
                "standard operating procedure, and permanently discontinue study drug for "
                "this subject{redose_clause}."
            ),
            rationale=(
                "The label identifies hypersensitivity as a known risk of this product; "
                "re-exposure after a systemic reaction risks a more severe reaction that "
                "cannot be predicted from the first."
            ),
            evidence="Drug label hypersensitivity language; CTCAE v5.0",
            applies_when="The label reports hypersensitivity, anaphylaxis or angioedema",
        ),
    ),
    "hypoglycemia": (
        SafetyRule(
            key="ind_hypoglycemia",
            scope="individual",
            tier=1,
            label="Hypoglycaemia",
            trigger=(
                "Plasma glucose below 54 mg/dL (3.0 mmol/L) - ADA Level 2, clinically "
                "significant hypoglycaemia - or any event requiring the assistance of "
                "another person for recovery (ADA Level 3, severe hypoglycaemia). A glucose "
                "below 70 mg/dL (3.9 mmol/L) is an alert value triggering repeat measurement "
                "and observation."
            ),
            action=(
                "Give oral carbohydrate, or intravenous glucose where the subject cannot "
                "swallow safely, and recheck glucose every 15 minutes until it is above 70 "
                "mg/dL on two consecutive readings. A Level 2 event is recorded and the "
                "subject is reviewed before any further dose; a Level 3 (severe) event means "
                "permanent discontinuation for this subject{redose_clause} and, being "
                "reported as a serious adverse event, independently triggers the cohort "
                "halting rule. Scheduled pharmacokinetic sampling continues unless it "
                "conflicts with treatment."
            ),
            rationale=(
                "Hypoglycaemia is the dose-limiting toxicity of insulin and insulin "
                "secretagogues, and in a euglycaemic clamp or fasted PK setting it is a "
                "foreseeable consequence of the procedure rather than a surprise, so the "
                "numeric trigger and the rescue must be pre-specified."
            ),
            evidence=(
                "ADA/EASD hypoglycaemia classification (Level 1 < 70 mg/dL, Level 2 < 54 "
                "mg/dL, Level 3 severe)"
            ),
            applies_when="The label reports hypoglycaemia (insulins, secretagogues, glucose-lowering agents)",
            thresholds={
                "level_1_alert_mg_dl": _HYPOGLYCEMIA_L1_MG_DL,
                "level_2_mg_dl": _HYPOGLYCEMIA_L2_MG_DL,
                "recheck_interval_minutes": 15,
            },
        ),
    ),
    "myelosuppression": (
        SafetyRule(
            key="ind_myelosuppression",
            scope="individual",
            tier=2,
            label="Haematological toxicity",
            trigger=(
                "Absolute neutrophil count below 1.0 x 10^9/L, platelet count below 50 x "
                "10^9/L, or haemoglobin below 8 g/dL - each CTCAE v5.0 Grade 3 - confirmed "
                "on a repeat sample."
            ),
            action=(
                "No further study drug for this subject{redose_clause}; repeat the count, "
                "obtain a haematology assessment, and follow until recovery to Grade <= 1."
            ),
            rationale=(
                "The label identifies marrow suppression as a known risk. The CTCAE Grade 3 "
                "boundaries are used directly so the trigger and the grading agree instead "
                "of drifting apart."
            ),
            evidence="CTCAE v5.0 haematological grading; drug label myelosuppression language",
            applies_when="The label reports neutropenia, thrombocytopenia or myelosuppression",
            thresholds={
                "anc_x10e9_l": _ANC_STOP_X10E9_L,
                "platelets_x10e9_l": _PLATELET_STOP_X10E9_L,
                "haemoglobin_g_dl": _HB_STOP_G_DL,
            },
        ),
    ),
    "respiratory_depression": (
        SafetyRule(
            key="ind_respiratory_depression",
            scope="individual",
            tier=1,
            label="Respiratory depression or excessive sedation",
            trigger=(
                "Respiratory rate below 8 breaths per minute, oxygen saturation below 90 "
                "percent on room air, or a subject who cannot be roused by voice or gentle "
                "touch."
            ),
            action=(
                "Begin immediate airway support and supplemental oxygen, administer the "
                "reversal agent held on the unit where one exists for this drug class, and "
                "permanently discontinue study drug for this subject{redose_clause}. The "
                "event is reported as a serious adverse event and therefore independently "
                "triggers the cohort halting rule."
            ),
            rationale=(
                "The label identifies respiratory depression as a known risk; it is the "
                "mechanism by which this drug class kills, and it progresses over minutes, "
                "so the trigger has to be a bedside number rather than an impression."
            ),
            evidence="Drug label respiratory depression language; CTCAE v5.0",
            applies_when="The label reports respiratory depression, apnoea, or opioid activity",
            thresholds={
                "respiratory_rate_per_min": _RESP_RATE_STOP_PER_MIN,
                "spo2_pct": _RESP_SPO2_STOP_PCT,
            },
        ),
    ),
    "nephrotoxicity": (
        SafetyRule(
            key="ind_acute_kidney_injury",
            scope="individual",
            tier=2,
            label="Acute kidney injury",
            trigger=(
                "Serum creatinine at or above 1.5 times the subject's pre-dose baseline, or "
                "a rise of at least 0.3 mg/dL within 48 hours - KDIGO stage 1 acute kidney "
                "injury - confirmed on a repeat sample within 24 hours."
            ),
            action=(
                "No further study drug for this subject{redose_clause}; ensure adequate "
                "hydration, withhold other nephrotoxic agents, and follow creatinine until "
                "it returns to baseline."
            ),
            rationale=(
                "The label identifies renal injury as a known risk. KDIGO stage 1 is the "
                "earliest boundary at which the change is unambiguously more than assay "
                "noise, and it uses the subject's own baseline rather than a population "
                "range."
            ),
            evidence="KDIGO 2012 Clinical Practice Guideline for Acute Kidney Injury",
            applies_when="The label reports nephrotoxicity, acute kidney injury or renal failure",
            thresholds={
                "creatinine_baseline_multiple": _CREATININE_AKI_MULTIPLE,
                "creatinine_rise_mg_dl": _CREATININE_AKI_RISE_MG_DL,
                "window_hours": _CREATININE_AKI_WINDOW_HOURS,
            },
        ),
    ),
    "lactic_acidosis": (
        SafetyRule(
            key="ind_lactic_acidosis",
            scope="individual",
            tier=1,
            label="Lactic acidosis",
            trigger=(
                "Venous lactate above 5 mmol/L with arterial pH below 7.35, or unexplained "
                "hyperventilation, abdominal pain or hypothermia prompting an immediate "
                "lactate and blood gas measurement."
            ),
            action=(
                "Permanently discontinue study drug for this subject{redose_clause} and begin "
                "supportive management immediately. The event is reported as a serious "
                "adverse event and therefore independently triggers the cohort halting rule."
            ),
            rationale=(
                "The label identifies lactic acidosis as a known risk. It carries high "
                "mortality and its early symptoms are non-specific, so the protocol pairs a "
                "hard laboratory boundary with an explicit list of symptoms that mandate "
                "measuring it."
            ),
            evidence="Drug label lactic acidosis language (biguanide class boxed warning)",
            applies_when="The label reports lactic acidosis",
            thresholds={"lactate_mmol_l": _LACTATE_STOP_MMOL_L, "arterial_ph": _ARTERIAL_PH_STOP},
        ),
    ),
    "seizure": (
        SafetyRule(
            key="ind_seizure",
            scope="individual",
            tier=1,
            label="Seizure",
            trigger="Any witnessed or reported seizure or convulsion after dosing.",
            action=(
                "Permanently discontinue study drug for this subject{redose_clause}, manage "
                "acutely per the unit's standard operating procedure, and obtain a "
                "neurological assessment. The event is reported as a serious adverse event "
                "and therefore independently triggers the cohort halting rule."
            ),
            rationale=(
                "The label identifies seizure risk. A single post-dose seizure in a healthy "
                "volunteer is never acceptable background morbidity, so it is treated as a "
                "study-level signal rather than an individual misfortune."
            ),
            evidence="Drug label seizure/convulsion language; CTCAE v5.0",
            applies_when="The label reports seizures or convulsions",
        ),
    ),
}


# ---------------------------------------------------------------------------
# Label cue detection
# ---------------------------------------------------------------------------
# Matched case-insensitively against the DailyMed label sections already fetched
# for the protocol (services/literature_service.py::_SPL_SECTIONS supplies
# clinical_pharmacology, pharmacokinetics, indications_usage,
# dosage_and_administration and how_supplied; the extra keys below are scanned
# too so that a richer label dict works without a code change).

_LABEL_SCAN_KEYS = (
    "title",
    "boxed_warning",
    "warnings_and_precautions",
    "warnings",
    "contraindications",
    "adverse_reactions",
    "indications_usage",
    "dosage_and_administration",
    "how_supplied",
    "clinical_pharmacology",
    "pharmacokinetics",
)

_RISK_CUES: dict[str, tuple[str, ...]] = {
    "hepatotoxicity": (
        r"hepatotoxic", r"liver (?:injury|failure|damage)", r"hepatic failure",
        r"hepatic necrosis", r"\bdili\b", r"jaundice",
        r"(?:elevat|increas)\w*\s+(?:in\s+)?(?:serum\s+)?(?:alt|ast|transaminase|aminotransferase)",
        r"(?:alt|ast|transaminase|aminotransferase)\w*\s+(?:elevat|increas)",
    ),
    "qt_prolongation": (
        r"qt[c]?\s*(?:interval\s*)?prolong", r"torsade", r"\bqtcf?\b",
        r"ventricular (?:arrhythmi|tachycardi|fibrillation)", r"proarrhythmi",
    ),
    "bleeding": (
        r"h(?:a)?emorrhag", r"\bbleeding\b", r"anticoagul", r"\binr\b",
        r"prothrombin time",
    ),
    "hypersensitivity": (
        r"anaphyla", r"hypersensitivity", r"angioedema",
        r"infusion[- ]related reaction", r"cytokine release",
        r"stevens[- ]johnson", r"toxic epidermal necrolysis",
    ),
    "hypoglycemia": (r"hypoglyc", r"\binsulin\b", r"blood glucose lowering"),
    "myelosuppression": (
        r"neutropen", r"myelosuppress", r"agranulocytosis", r"thrombocytopen",
        r"aplastic an(?:a)?emia", r"bone marrow suppression",
    ),
    "respiratory_depression": (
        r"respiratory depression", r"\bapn(?:o)?ea\b", r"\bopioid\b",
        r"respiratory arrest",
    ),
    "nephrotoxicity": (
        r"nephrotoxic", r"acute kidney injury", r"renal failure",
        r"acute renal", r"tubular necrosis",
    ),
    "lactic_acidosis": (r"lactic acidosis",),
    "seizure": (r"\bseizure", r"convulsi", r"status epilepticus"),
}

# Explicit boxed-warning phrases only. A looser cue (for example the bare word
# "warning") would fire on the ordinary "WARNINGS AND PRECAUTIONS" heading that
# every label carries, which would escalate every study and make the escalation
# meaningless.
_BOXED_WARNING_CUES: tuple[str, ...] = (
    r"boxed warning",
    r"black[ -]box(?:ed)? warning",
    r"see full prescribing information for complete boxed warning",
)

# Parenteral administration, read off the label's own administration text rather
# than inferred from the drug name.
_PARENTERAL_CUES: tuple[str, ...] = (
    r"subcutaneous", r"intravenous", r"intramuscular", r"\binfusion\b",
    r"prefilled syringe", r"pre-filled syringe", r"autoinjector",
    r"auto-injector", r"\binject", r"\bvial\b", r"\bsc\b", r"\biv\b",
)

_ORAL_CUES: tuple[str, ...] = (
    r"\boral", r"\btablet", r"\bcapsule", r"by mouth", r"swallow",
)


def _label_text(label: Optional[dict[str, str]]) -> str:
    """Concatenate the label sections worth scanning, lower-cased."""
    if not label:
        return ""
    return " ".join(str(label.get(k, "") or "") for k in _LABEL_SCAN_KEYS).lower()


def _detect_risk_topics(text: str) -> list[str]:
    """Which risk topics the label gives positive evidence for (stable order)."""
    if not text:
        return []
    return [key for key, patterns in _RISK_CUES.items()
            if any(re.search(p, text) for p in patterns)]


def _detect_boxed_warning(label: Optional[dict[str, str]]) -> tuple[bool, str, str]:
    """(detected, matched phrase, label section it was found in).

    Deliberately reports WHERE the match came from: an inspector reading the
    protocol should be able to see that the escalation rests on the label rather
    than on an assumption.
    """
    if not label:
        return False, "", ""
    for key in _LABEL_SCAN_KEYS:
        value = str(label.get(key, "") or "").lower()
        if not value:
            continue
        for pattern in _BOXED_WARNING_CUES:
            match = re.search(pattern, value)
            if match:
                return True, match.group(0), key
    return False, "", ""


def _detect_route(label: Optional[dict[str, str]], study_type: str, route: Optional[str]) -> str:
    """'parenteral' | 'oral' | 'unknown', preferring an explicit caller value."""
    if route:
        low = route.strip().lower()
        if any(w in low for w in ("sc", "subcut", "iv", "intraven", "intramus", "inject", "infus")):
            return "parenteral"
        if any(w in low for w in ("oral", "po", "tablet", "capsule")):
            return "oral"
    text = _label_text(label)
    parenteral = any(re.search(p, text) for p in _PARENTERAL_CUES)
    oral = any(re.search(p, text) for p in _ORAL_CUES)
    if parenteral and not oral:
        return "parenteral"
    if oral and not parenteral:
        return "oral"
    if parenteral and oral:
        # A label that describes both (for example an oral and an IV form) is
        # resolved by the study type: this app's biosimilar pathway is always a
        # biological product given parenterally.
        return "parenteral" if study_type == "biosimilar_pk" else "oral"
    # No usable label text: fall back to the study type, which is always known.
    return "parenteral" if study_type == "biosimilar_pk" else "oral"


# ---------------------------------------------------------------------------
# Count scaling
# ---------------------------------------------------------------------------


def _scaled_count(
    n: Optional[int],
    fraction: float,
    floor: int,
    cap: Optional[int] = None,
    uplift: int = 0,
) -> int:
    """Cumulative subject count that trips a cohort halting rule.

    Scaled to the planned dosed N, then bounded: the floor stops a small study
    halting on a single idiosyncratic event, and the cap stops a large study
    from arithmetically tolerating an absolute number of severe events that no
    reviewer would accept. With no N available the floor is used, which is the
    conservative (earliest-halting) end of the range.
    """
    base = floor if not n or n <= 0 else max(floor, int(math.ceil(fraction * n)))
    if cap is not None:
        base = min(base, cap)
    return base + uplift


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_RENDERED_FIELDS = ("label", "trigger", "action", "applies_when")


def _render(rule: SafetyRule, subs: dict[str, Any], added_because: str = "") -> dict[str, Any]:
    """Fill the per-study placeholders and return a JSON-able rule dict."""
    d = rule.to_dict()
    for name in _RENDERED_FIELDS:
        d[name] = d[name].format(**subs)
    if rule.count_key:
        d["thresholds"] = {
            **d["thresholds"],
            "subject_count": subs[rule.count_key],
            "denominator_planned_dosed": subs["n"],
        }
    if added_because:
        d["added_because"] = added_because
    return d


def _redose_clause(design_key: str) -> str:
    """What 'no further study drug' actually means for THIS design.

    In a crossover the subject has further doses scheduled, so withholding them
    is the operative consequence. In a parallel study there is only one
    administration, so the same phrase would be empty words - the consequence is
    management and follow-up instead. Getting this wrong produces protocol text
    that promises to withhold a dose that was never going to be given.
    """
    if design_key in ("crossover", "replicate"):
        return (
            " and is not dosed in any subsequent treatment period (the subject does not "
            "cross over)"
        )
    return (
        " (dosing is complete after the single administration in this parallel-group "
        "design, so the consequence is management, observation and follow-up rather "
        "than withholding a later dose)"
    )


# ---------------------------------------------------------------------------
# Discharge criteria
# ---------------------------------------------------------------------------


def _discharge_criteria(
    route: str, design_key: str, *, omit: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Objective conditions for releasing a dosed subject from confinement.

    Every line is something a nurse can verify at the bedside and record. The
    physician authorisation is listed as a criterion in its own right because a
    protocol that lists only clinical parameters lets a subject leave on a
    nurse's reading of a checklist, which is not what ICH E6 investigator
    responsibility means.

    ``omit`` drops criteria by key. It exists so a discharge gate cannot outlive
    the stopping rule it belongs to: when QT gating is suppressed for a
    monoclonal antibody, a QTcF discharge threshold would be the only mention of
    QT left in the protocol - a bedside check with no rule behind it.
    """
    vitals = (
        "Vital signs within the following ranges on two measurements at least "
        "30 minutes apart, the later one taken with the subject seated or standing: "
        "systolic blood pressure 90-140 mmHg; diastolic blood pressure 50-90 mmHg; "
        "pulse 45-100 beats per minute; respiratory rate 12-20 per minute; oxygen "
        "saturation at least 95 percent on room air; tympanic or oral temperature no "
        "higher than 37.8 degrees Celsius."
    )
    criteria: list[dict[str, Any]] = [
        {
            "key": "observation_period",
            "criterion": (
                "At least 24 hours have elapsed since dosing, and all in-unit scheduled "
                "assessments for the period are complete."
            ),
            "objective": True,
            "thresholds": {"min_post_dose_hours": _MIN_POST_DOSE_OBSERVATION_HOURS},
        },
        {
            "key": "vital_signs",
            "criterion": vitals,
            "objective": True,
            "thresholds": {
                "systolic_mmHg": list(_DISCHARGE_SBP_MMHG),
                "diastolic_mmHg": list(_DISCHARGE_DBP_MMHG),
                "pulse_bpm": list(_DISCHARGE_PULSE_BPM),
                "respiratory_rate_per_min": list(_DISCHARGE_RR_PER_MIN),
                "spo2_min_pct": _DISCHARGE_SPO2_MIN_PCT,
                "temperature_max_c": _DISCHARGE_TEMP_MAX_C,
                "confirm_interval_minutes": _DISCHARGE_VITALS_CONFIRM_INTERVAL_MIN,
            },
        },
        {
            "key": "no_orthostatic_intolerance",
            "criterion": (
                "No symptomatic orthostatic intolerance: on standing after 5 minutes "
                "supine, systolic blood pressure falls by no more than 20 mmHg and "
                "diastolic by no more than 10 mmHg, without dizziness or presyncope."
            ),
            "objective": True,
            "thresholds": {
                "systolic_drop_max_mmHg": _DISCHARGE_ORTHOSTATIC_SBP_DROP_MMHG,
                "diastolic_drop_max_mmHg": _DISCHARGE_ORTHOSTATIC_DBP_DROP_MMHG,
            },
        },
        {
            "key": "ambulatory",
            "criterion": (
                "The subject is alert, fully orientated, and ambulatory without assistance."
            ),
            "objective": True,
        },
        {
            "key": "oral_intake",
            "criterion": (
                "The subject is tolerating oral fluids and solid food, with no vomiting in "
                "the preceding 4 hours."
            ),
            "objective": True,
        },
        {
            "key": "no_ongoing_related_grade2",
            "criterion": (
                "No ONGOING adverse event assessed as related to study drug at CTCAE v5.0 "
                "Grade 2 or higher. An ongoing related Grade 1 event is acceptable provided "
                "the Investigator judges outpatient follow-up safe and records that "
                "judgement."
            ),
            "objective": True,
        },
        {
            "key": "ecg",
            "criterion": (
                "Where a post-dose ECG has been recorded, QTcF is below 480 ms and within "
                "30 ms of the pre-dose baseline, and there is no new clinically significant "
                "abnormality."
            ),
            "objective": True,
            "thresholds": {
                "qtcf_max_ms": _DISCHARGE_QTCF_MAX_MS,
                "qtcf_delta_max_ms": _QTCF_DELTA_MONITOR_UNTIL_MS,
            },
        },
        {
            "key": "laboratory",
            "criterion": (
                "Any clinically significant laboratory abnormality identified during "
                "confinement has been repeated and is resolving, or the Investigator has "
                "documented a follow-up plan; no unresolved finding that meets a stopping "
                "rule is outstanding."
            ),
            "objective": True,
        },
        {
            "key": "final_pk_sample",
            "criterion": (
                "The final scheduled in-unit pharmacokinetic sample for the period has been "
                "drawn, labelled, time-stamped against the actual dosing time, and "
                "reconciled against the sampling schedule; any ambulatory samples still due "
                "have been scheduled with the subject."
            ),
            "objective": True,
        },
        {
            "key": "physician_authorisation",
            "criterion": (
                "Documented written authorisation to discharge by the Principal "
                "Investigator or a medically-qualified delegate listed on the delegation "
                "log, recorded in the source document with the date, the time, and the "
                "signature. No dosed subject leaves the unit without it."
            ),
            "objective": True,
            "mandatory": True,
        },
    ]

    if route == "parenteral":
        criteria.insert(1, {
            "key": "post_injection_observation",
            "criterion": (
                "The subject has remained under direct observation, with resuscitation "
                "equipment and a physician immediately available, for at least 4 hours "
                "after the injection or infusion, and the injection site has been inspected "
                "and graded at that time."
            ),
            "objective": True,
            "thresholds": {"direct_observation_hours": _INJECTION_DIRECT_OBSERVATION_HOURS},
        })

    if omit:
        criteria = [c for c in criteria if c.get("key") not in omit]

    return {
        "criteria": criteria,
        "authorisation": (
            "Discharge requires documented written authorisation by the Principal "
            "Investigator or a medically-qualified delegate named on the delegation log. "
            "The authorisation records the date and time and confirms that every criterion "
            "above was met."
        ),
        "min_post_dose_observation_hours": _MIN_POST_DOSE_OBSERVATION_HOURS,
        "if_not_met": (
            "A subject who does not meet every criterion remains in the unit under "
            "observation until the criteria are met, or is transferred to an appropriate "
            "medical facility. The reason for the extended stay, the assessments performed "
            "and the eventual discharge decision are recorded in the source document. "
            "Failure to meet the criteria is not by itself a withdrawal - the individual "
            "stopping rules decide that separately."
        ),
        "readmission": (
            "The subject returns for the ambulatory pharmacokinetic samples and the "
            "end-of-study visit as scheduled; the discharge record states the next required "
            "contact and the 24-hour unit telephone number given to the subject."
        ),
        "design_note": (
            "In this multi-period design, discharge from one period does not authorise "
            "dosing in the next: the subject must additionally satisfy the pre-dose "
            "eligibility check and have no ongoing related Grade 2 or higher event at the "
            "start of the following period."
            if design_key in ("crossover", "replicate") else
            "In this parallel-group design each subject is dosed once, so discharge from "
            "confinement is followed by ambulatory pharmacokinetic sampling and the "
            "end-of-study visit only."
        ),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def recommend_safety_gating(
    *,
    study_type: Optional[str] = None,
    design_key: Optional[str] = None,
    n_enrolled: Optional[int] = None,
    n_evaluable: Optional[int] = None,
    label: Optional[dict[str, str]] = None,
    is_nti: Optional[bool] = None,
    route: Optional[str] = None,
    dropout_rate: Optional[float] = None,
    healthy_volunteers: bool = True,
    is_biologic: bool = False,
) -> dict[str, Any]:
    """Build the pre-specified safety gating rules for THIS study.

    Parameters
    ----------
    study_type:
        'BE' | 'biosimilar_pk' | 'food_effect'. A biosimilar (parenteral
        biological) study gets hypersensitivity/anaphylaxis and injection-site
        gating that an oral small-molecule BE study does not; an oral study gets
        the emesis data-handling gate instead.
    design_key:
        'crossover' | 'parallel' | 'replicate' - from the sample-size engine.
        Decides what "no further study drug" means for an individual subject.
    n_enrolled / n_evaluable:
        Planned dosed subjects. ``n_enrolled`` is preferred because it is the
        number actually exposed. The cumulative cohort halting counts are scaled
        to it; with neither available the counts fall back to their floors, which
        is the earliest-halting (conservative) end.
    label:
        The trimmed DailyMed label dict already assembled for the protocol. Risk
        topics and the presence of a boxed warning are read from this text, so
        the escalations rest on the real label rather than on recall.
    is_nti:
        Narrow-therapeutic-index status; tightens the cohort tally to a single
        related severe event and adds sentinel dosing.
    route:
        Optional explicit route override ('SC', 'IV', 'oral', ...). When absent
        the route is inferred from the label's administration text.
    dropout_rate:
        The design's dropout allowance (fraction). When supplied it sets the
        AE-withdrawal halting count, so the safety rule and the powering
        assumption agree instead of contradicting each other.
    healthy_volunteers:
        True for a healthy-volunteer study (no prospect of therapeutic benefit,
        so tolerance for related severe events is minimal). False for a patient
        population, where background disease morbidity justifies one additional
        event in the cumulative counts.

    Returns
    -------
    dict, JSON-serialisable for storage in population_config (JSONB), with
    ``grading_scale``, ``individual_rules``, ``cohort_rules``,
    ``discharge_criteria``, ``escalations``, ``label_findings``,
    ``assumptions``, ``thresholds`` and ``summary``.
    """
    st = (study_type or "BE").strip()
    design = (design_key or "crossover").strip().lower()
    n_dosed = n_enrolled or n_evaluable or None
    detected_route = _detect_route(label, st, route)

    text = _label_text(label)
    risk_topics = _detect_risk_topics(text)
    boxed, boxed_phrase, boxed_section = _detect_boxed_warning(label)

    uplift = 0 if healthy_volunteers else _PATIENT_HALT_COUNT_UPLIFT

    # Cumulative cohort counts, scaled to the planned dosed N.
    k_g3 = _scaled_count(
        n_dosed, _HALT_RELATED_G3_FRACTION, _HALT_RELATED_G3_FLOOR,
        _HALT_RELATED_G3_CAP, uplift,
    )
    if is_nti:
        # NTI: a single related severe event is already a study-level signal,
        # because the margin between exposure and harm is what defines the class.
        k_g3 = _NTI_G3_HALT_COUNT
    k_cluster = _scaled_count(
        n_dosed, _HALT_CLUSTER_G2_FRACTION, _HALT_CLUSTER_G2_FLOOR,
        _HALT_CLUSTER_G2_CAP, uplift,
    )
    withdrawal_fraction = (
        dropout_rate if dropout_rate and dropout_rate > 0 else _HALT_AE_WITHDRAWAL_FRACTION
    )
    k_withdraw = _scaled_count(
        n_dosed, withdrawal_fraction, _HALT_AE_WITHDRAWAL_FLOOR, None, uplift,
    )
    k_qt = 1 if (is_nti or "qt_prolongation" in risk_topics) else _HALT_QT_SUBJECTS

    n_phrase = (
        f"among the {n_dosed} planned to be dosed"
        if n_dosed else "among the subjects dosed to date"
    )
    subs: dict[str, Any] = {
        "k_g3": k_g3,
        "k_qt": k_qt,
        "k_cluster": k_cluster,
        "k_withdraw": k_withdraw,
        "n": n_dosed,
        "n_phrase": n_phrase,
        "redose_clause": _redose_clause(design),
    }

    # Assemble: core rules, then route-specific, then label-driven risk rules,
    # then context escalations. Deduplicated on key, first entry wins, so a core
    # rule is never displaced by a narrower one carrying the same key.
    rendered: dict[str, dict[str, Any]] = {}
    escalations: list[dict[str, str]] = []

    # Rules that exist only because a small molecule has a hepatic metabolic
    # burden. A therapeutic protein is catabolised to amino acids by ubiquitous
    # proteolysis; it does not produce the reactive metabolites that drive
    # idiosyncratic DILI, and Hy's Law was derived from - and is validated for -
    # small-molecule hepatocellular injury. Carrying these into a biosimilar
    # protocol pads the safety section with thresholds that will never be met and
    # trains reviewers to skim the rules that DO matter (anaphylaxis,
    # injection-site reactions, immunogenicity).
    _SMALL_MOLECULE_ONLY = {
        "ind_hys_law",
        "coh_hys_law",
        "ind_hepatic_discontinuation",
        "ind_hepatic_predose_gate",
    }
    # QT gating exists because small molecules block the hERG potassium channel -
    # a lipophilic-small-molecule mechanism. A monoclonal antibody is far too
    # large to enter the channel pore, so routine QT stopping rules are dead text
    # for a biologic. Kept only when the label itself reports a QT signal, which
    # _detect_risk_topics will have found.
    if is_biologic and "qt_prolongation" not in risk_topics and not is_nti:
        _SMALL_MOLECULE_ONLY |= {"ind_qt_prolongation", "coh_qt_count", "ind_qt_predose_gate"}
    suppressed: list[str] = []

    def _add(rule: SafetyRule, because: str = "") -> None:
        if rule.key in rendered:
            return
        if is_biologic and rule.key in _SMALL_MOLECULE_ONLY:
            suppressed.append(rule.key)
            return
        rendered[rule.key] = _render(rule, subs, because)

    for rule in _CORE_INDIVIDUAL + _CORE_COHORT:
        _add(rule)

    if detected_route == "parenteral":
        for rule in _INJECTABLE_RULES:
            _add(rule, "Parenteral administration - systemic hypersensitivity and "
                       "injection-site reactions are foreseeable for this route")
        escalations.append({
            "key": "parenteral_route",
            "reason": (
                "The product is administered parenterally"
                + (" (study type biosimilar_pk)" if st == "biosimilar_pk" else "")
                + "."
            ),
            "effect": (
                "Anaphylaxis and injection-site gating rules added; direct observation "
                "with resuscitation capability for at least 4 hours after injection is "
                "required before discharge can be considered."
            ),
        })
    else:
        for rule in _ORAL_RULES:
            _add(rule, "Oral administration - emesis inside the absorption window "
                       "invalidates the exposure comparison")

    for topic in risk_topics:
        for rule in _RISK_RULES.get(topic, ()):
            _add(rule, f"Label text reports {topic.replace('_', ' ')}")
    if risk_topics:
        escalations.append({
            "key": "label_risk_topics",
            "reason": (
                "The approved label reports: " + ", ".join(
                    t.replace("_", " ") for t in risk_topics) + "."
            ),
            "effect": (
                "Targeted pre-dose gates, monitoring and stopping boundaries added for "
                "each reported risk."
            ),
        })

    needs_sentinel = bool(is_nti) or boxed or (
        detected_route == "parenteral" and st == "biosimilar_pk"
    )
    if needs_sentinel:
        _add(_ESCALATION_RULES[0], "Sentinel dosing escalation")

    if is_nti:
        escalations.append({
            "key": "narrow_therapeutic_index",
            "reason": "The product is a narrow-therapeutic-index drug.",
            "effect": (
                f"Cumulative related Grade >= 3 halting count reduced to "
                f"{_NTI_G3_HALT_COUNT} subject; QT halting count reduced to {k_qt}; "
                f"sentinel dosing of the first {_SENTINEL_COHORT_N} subjects with "
                f"{_SENTINEL_OBSERVATION_HOURS:.0f} hours of review before the remainder "
                "are dosed."
            ),
        })
    if boxed:
        escalations.append({
            "key": "boxed_warning",
            "reason": (
                f"A boxed warning was detected in the approved label "
                f"(matched '{boxed_phrase}' in the {boxed_section} section)."
            ),
            "effect": (
                f"Sentinel dosing of the first {_SENTINEL_COHORT_N} subjects with "
                f"{_SENTINEL_OBSERVATION_HOURS:.0f} hours of documented safety review "
                "before any further subject is dosed; the boxed-warning hazard is covered "
                "by the matching risk rules above and is stated explicitly in the "
                "risk/benefit section."
            ),
        })
    if is_biologic and suppressed:
        escalations.append({
            "key": "biologic_modality",
            "reason": (
                "The product is a therapeutic protein, catabolised to amino acids by "
                "ubiquitous proteolysis rather than metabolised hepatically."
            ),
            "effect": (
                "Hy's Law and the FDA DILI liver-chemistry discontinuation thresholds are "
                "NOT applied: they were derived for small-molecule hepatocellular injury "
                "and cannot be met by this mechanism. Liver chemistry remains part of "
                "routine safety monitoring and any clinically significant abnormality is "
                "still captured by the adverse-event and Investigator-judgement rules. "
                "Gating focuses instead on hypersensitivity/anaphylaxis, injection-site "
                "reactions and immunogenicity."
            ),
        })
    if not healthy_volunteers:
        escalations.append({
            "key": "patient_population",
            "reason": "The study enrols patients rather than healthy volunteers.",
            "effect": (
                f"Cumulative halting counts raised by {_PATIENT_HALT_COUNT_UPLIFT} to allow "
                "for background disease morbidity; only events assessed as RELATED are "
                "counted."
            ),
        })

    individual_rules = [r for r in rendered.values() if r["scope"] == "individual"]
    cohort_rules = [r for r in rendered.values() if r["scope"] == "cohort"]
    order = {r.key: i for i, r in enumerate(
        _CORE_INDIVIDUAL + _CORE_COHORT + _INJECTABLE_RULES + _ORAL_RULES + _ESCALATION_RULES
    )}
    for bucket in (individual_rules, cohort_rules):
        bucket.sort(key=lambda d: (d["tier"], order.get(d["key"], 999)))

    result: dict[str, Any] = {
        "study_type": st,
        "design_key": design,
        "route": detected_route,
        "healthy_volunteers": healthy_volunteers,
        "n_dosed_planned": n_dosed,
        "denominator_basis": (
            "planned enrolled (dosed) subjects" if n_enrolled
            else "planned evaluable subjects" if n_evaluable
            else "not available - halting counts use their floors"
        ),
        "grading_scale": {
            "name": "CTCAE v5.0",
            "citation": (
                "Common Terminology Criteria for Adverse Events version 5.0, "
                "US Department of Health and Human Services / NCI, published 27 November 2017"
            ),
            "grades": [g.to_dict() for g in _CTCAE_GRADES],
            "severity_vs_seriousness": _SEVERITY_VS_SERIOUSNESS,
            "causality_basis": _CAUSALITY_BASIS,
        },
        "individual_rules": individual_rules,
        "cohort_rules": cohort_rules,
        "counts": {
            "related_grade3_halt": k_g3,
            "qt_boundary_subjects_halt": k_qt,
            "same_event_grade2_cluster_halt": k_cluster,
            "ae_withdrawal_halt": k_withdraw,
            "withdrawal_fraction_used": withdrawal_fraction,
        },
        "thresholds": {
            "hys_law": {
                "alt_or_ast_uln_multiple": _HYS_LAW_AMINOTRANSFERASE_ULN_X,
                "total_bilirubin_uln_multiple": _HYS_LAW_BILIRUBIN_ULN_X,
                "alkaline_phosphatase_max_uln_multiple": _HYS_LAW_ALP_MAX_ULN_X,
                "alternative_cause_excluded_required": True,
            },
            "hepatic_discontinuation": {
                "alt_ast_uln_multiple": _ALT_DISCONTINUE_ULN_X,
                "alt_ast_persistent_uln_multiple": _ALT_PERSISTENT_ULN_X,
                "persistence_days": _ALT_PERSISTENT_DAYS,
                "alt_ast_symptomatic_uln_multiple": _ALT_SYMPTOMATIC_ULN_X,
            },
            "qt": {
                "qtcf_absolute_ms": _QTCF_ABSOLUTE_STOP_MS,
                "qtcf_delta_from_baseline_ms": _QTCF_DELTA_STOP_MS,
                "monitor_until_qtcf_below_ms": _QTCF_MONITOR_UNTIL_MS,
                "confirmatory_repeat_minutes": _ECG_REPEAT_WINDOW_MIN,
            },
            "vital_sign_stop": {
                "systolic_mmHg": list(_STOP_SBP_MMHG),
                "diastolic_mmHg": list(_STOP_DBP_MMHG),
                "pulse_bpm": list(_STOP_PULSE_BPM),
                "spo2_min_pct": _STOP_SPO2_PCT,
                "temperature_max_c": _STOP_TEMP_C,
            },
            "discharge_vitals": {
                "systolic_mmHg": list(_DISCHARGE_SBP_MMHG),
                "diastolic_mmHg": list(_DISCHARGE_DBP_MMHG),
                "pulse_bpm": list(_DISCHARGE_PULSE_BPM),
                "respiratory_rate_per_min": list(_DISCHARGE_RR_PER_MIN),
                "spo2_min_pct": _DISCHARGE_SPO2_MIN_PCT,
                "temperature_max_c": _DISCHARGE_TEMP_MAX_C,
                "qtcf_max_ms": _DISCHARGE_QTCF_MAX_MS,
            },
        },
        # A discharge gate must not outlive the stopping rule it belongs to: with
        # QT gating suppressed for a monoclonal antibody, a QTcF discharge
        # threshold would be the only QT mention left in the document.
        "discharge_criteria": _discharge_criteria(
            detected_route, design,
            omit=frozenset({"ecg"}) if "ind_qt_prolongation" in suppressed else frozenset(),
        ),
        "escalations": escalations,
        # Which rules were withheld and why. Exposed rather than silently dropped
        # so a reviewer can see that (for example) Hy's Law was considered and
        # deliberately not applied to a therapeutic protein - and so _summary can
        # avoid describing boundaries this study does not have.
        "suppressed_rules": suppressed,
        "label_findings": {
            "risk_topics": risk_topics,
            "boxed_warning_detected": boxed,
            "boxed_warning_evidence": boxed_phrase,
            "boxed_warning_section": boxed_section,
            "sections_scanned": [k for k in _LABEL_SCAN_KEYS if (label or {}).get(k)],
            "note": (
                "Risk topics and boxed-warning status are detected from the retrieved "
                "label text. The SPL sections retrieved for this application do not "
                "always include the boxed-warning section, so a negative result means "
                "'not found in the retrieved text', NOT 'this product has no boxed "
                "warning'. The Investigator confirms against the current full "
                "prescribing information."
            ),
        },
        # Drop the QT halting-count assumption alongside the QT rules themselves -
        # disclosing a convention for a boundary the protocol no longer contains
        # is exactly the dead text the suppression exists to remove.
        "assumptions": _assumptions(
            withdrawal_fraction,
            omit=frozenset({"qt_halt_subject_count"})
            if "ind_qt_prolongation" in suppressed else frozenset(),
        ),
        "review_and_resumption": (
            "Every halt is recorded with its trigger, the date and time, and the subjects "
            "affected. Dosing resumes only after a documented joint written review by the "
            "Sponsor Medical Monitor and the Principal Investigator, with notification to "
            "the IRB/IEC in accordance with its reporting requirements. Where resumption "
            "would change the dose, the population, the eligibility criteria or the "
            "monitoring plan, a protocol amendment is approved before any further subject "
            "is dosed."
        ),
        "summary": "",
    }
    result["summary"] = _summary(result, is_nti, boxed)
    return result


def _assumptions(
    withdrawal_fraction: float, *, omit: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """The values no guideline fixes, surfaced so the protocol can disclose them.

    A number the protocol states as if it were a regulatory requirement, when it
    is really unit practice, is the kind of thing that unravels at an inspection.
    Each entry names the value and says what it rests on.

    ``omit`` drops entries by key. An assumption behind a suppressed rule must go
    with it: disclosing a QT halting count for a monoclonal antibody that has no
    QT rule reintroduces the dead text the suppression removed.
    """
    entries = [
        {
            "key": "related_grade3_halt_fraction",
            "value": f"{_HALT_RELATED_G3_FRACTION:.0%} of planned dosed subjects, "
                     f"minimum {_HALT_RELATED_G3_FLOOR}, maximum {_HALT_RELATED_G3_CAP}",
            "basis": (
                "No guideline supplies a numeric halting fraction for a healthy-volunteer "
                "BE/PK study. The fraction is bounded so the rule behaves sensibly at both "
                "small and large N."
            ),
        },
        {
            "key": "same_event_cluster_halt_fraction",
            "value": f"{_HALT_CLUSTER_G2_FRACTION:.0%} of planned dosed subjects, "
                     f"minimum {_HALT_CLUSTER_G2_FLOOR}, maximum {_HALT_CLUSTER_G2_CAP}",
            "basis": "Unit practice; targets a batch or procedural cause rather than severity.",
        },
        {
            "key": "ae_withdrawal_halt_fraction",
            "value": f"{withdrawal_fraction:.0%} of planned dosed subjects, "
                     f"minimum {_HALT_AE_WITHDRAWAL_FLOOR}",
            "basis": (
                "Tied to the design's dropout allowance where one is available, so the "
                "safety boundary and the powering assumption cannot contradict each other."
            ),
        },
        {
            "key": "qt_halt_subject_count",
            "value": f"{_HALT_QT_SUBJECTS} subjects meeting the individual QT boundary "
                     "(reduced to 1 for a narrow-therapeutic-index drug or a label reporting "
                     "QT prolongation)",
            "basis": (
                "ICH E14 fixes the individual thresholds but not how many subjects "
                "constitute a study-level signal."
            ),
        },
        {
            "key": "vital_sign_action_limits",
            "value": (
                f"SBP {_STOP_SBP_MMHG[0]}-{_STOP_SBP_MMHG[1]} mmHg, "
                f"DBP {_STOP_DBP_MMHG[0]}-{_STOP_DBP_MMHG[1]} mmHg, "
                f"pulse {_STOP_PULSE_BPM[0]}-{_STOP_PULSE_BPM[1]} bpm, "
                f"SpO2 >= {_STOP_SPO2_PCT} percent, "
                f"temperature < {_STOP_TEMP_C} degrees Celsius"
            ),
            "basis": "Conventional Phase I unit action limits; no guideline fixes them.",
        },
        {
            "key": "discharge_vital_sign_ranges",
            "value": (
                f"SBP {_DISCHARGE_SBP_MMHG[0]}-{_DISCHARGE_SBP_MMHG[1]} mmHg, "
                f"DBP {_DISCHARGE_DBP_MMHG[0]}-{_DISCHARGE_DBP_MMHG[1]} mmHg, "
                f"pulse {_DISCHARGE_PULSE_BPM[0]}-{_DISCHARGE_PULSE_BPM[1]} bpm, "
                f"respiratory rate {_DISCHARGE_RR_PER_MIN[0]}-{_DISCHARGE_RR_PER_MIN[1]}/min, "
                f"SpO2 >= {_DISCHARGE_SPO2_MIN_PCT} percent, "
                f"temperature <= {_DISCHARGE_TEMP_MAX_C} degrees Celsius"
            ),
            "basis": (
                "Conventional Phase I discharge ranges, deliberately narrower than the "
                "stopping limits."
            ),
        },
        {
            "key": "min_post_dose_observation_hours",
            "value": f"{_MIN_POST_DOSE_OBSERVATION_HOURS:.0f} hours",
            "basis": (
                "Unit practice. In most periods the binding constraint is the last in-unit "
                "PK sample, which falls later than this."
            ),
        },
        {
            "key": "post_injection_direct_observation_hours",
            "value": f"{_INJECTION_DIRECT_OBSERVATION_HOURS:.0f} hours (parenteral products)",
            "basis": (
                "Anaphylaxis after parenteral administration is overwhelmingly an early "
                "event (minutes to about 2 hours); 4 hours provides margin."
            ),
        },
        {
            "key": "sentinel_observation_hours",
            "value": f"{_SENTINEL_COHORT_N} sentinel subjects, "
                     f"{_SENTINEL_OBSERVATION_HOURS:.0f} hours of review",
            "basis": (
                "EMA EMEA/CHMP/SWP/28367/07 Rev 1 requires sentinel dosing but does not fix "
                "the interval; 24 hours captures the first full laboratory and ECG set."
            ),
        },
    ]
    if omit:
        entries = [e for e in entries if e.get("key") not in omit]
    return entries


def _summary(result: dict[str, Any], is_nti: Optional[bool], boxed: bool) -> str:
    """One-paragraph plain statement for the protocol narrative."""
    counts = result["counts"]
    n_dosed = result["n_dosed_planned"]
    denom = f"among the {n_dosed} planned to be dosed" if n_dosed else "among those dosed"
    lead = (
        "Safety gating is pre-specified at two independent levels. INDIVIDUAL stopping "
        "rules decide whether one subject receives any further study drug"
        + (" (in this multi-period design, whether the subject is dosed in a subsequent "
           "period)" if result["design_key"] in ("crossover", "replicate")
           else " (in this parallel-group design each subject is dosed once, so these "
                "rules govern management and follow-up)")
        + ". COHORT halting rules decide whether ANY further subject is dosed; they are "
          "cumulative counts and no halt is lifted without documented Sponsor and "
          "Investigator review."
    )
    # The summary must describe the rules this study ACTUALLY has. Naming a QT
    # boundary or a Hy's Law case for a monoclonal antibody - whose QT and
    # hepatic rules were suppressed as mechanistically inapplicable - would put
    # back, in the one paragraph everybody reads, exactly the dead text the
    # suppression removed.
    suppressed_keys = set(result.get("suppressed_rules") or ())
    qt_gated = "ind_qt_prolongation" not in suppressed_keys
    hepatic_gated = "ind_hys_law" not in suppressed_keys

    qt_clause = (
        f"if {counts['qt_boundary_subjects_halt']} or more meet the QT boundary (QTcF > "
        f"{_QTCF_ABSOLUTE_STOP_MS:.0f} ms or an increase > {_QTCF_DELTA_STOP_MS:.0f} ms "
        f"from baseline), " if qt_gated else ""
    )
    counted = (
        f" Dosing of further subjects halts if {counts['related_grade3_halt']} or more "
        f"subjects {denom} experience a related Grade >= 3 event, "
        f"{qt_clause}"
        f"if {counts['same_event_grade2_cluster_halt']} or more report the "
        f"same related Grade >= 2 event, or if {counts['ae_withdrawal_halt']} or more are "
        "withdrawn for adverse events."
    )
    hys_clause = (
        ", or a single confirmed Hy's Law case (ALT or AST >= "
        f"{_HYS_LAW_AMINOTRANSFERASE_ULN_X:.0f} x ULN with total bilirubin >= "
        f"{_HYS_LAW_BILIRUBIN_ULN_X:.0f} x ULN, ALP < "
        f"{_HYS_LAW_ALP_MAX_ULN_X:.0f} x ULN and no alternative explanation)"
        if hepatic_gated else ""
    )
    immediate = (
        " Independently of any count, a single related Grade 4 or Grade 5 event, a single "
        f"related serious adverse event{hys_clause} halts dosing "
        "immediately at any sample size."
    )
    stakes = ""
    if is_nti:
        stakes += (
            " Because this is a narrow-therapeutic-index drug, the cumulative related "
            "severe-event count is reduced to a single subject and the first "
            f"{_SENTINEL_COHORT_N} subjects are dosed as a sentinel group."
        )
    if boxed:
        stakes += (
            " The approved label carries a boxed warning, so sentinel dosing and the "
            "matching hazard-specific stopping rules apply."
        )
    if result["healthy_volunteers"]:
        stakes += (
            " Subjects derive no therapeutic benefit from participation, which is why the "
            "tolerance for related severe events is set this low."
        )
    tail = (
        " A dosed subject is discharged from confinement only when every objective "
        "criterion is met and a physician has authorised the discharge in writing."
    )
    return lead + counted + immediate + stakes + tail


def safety_gating_directive(gating: dict[str, Any]) -> str:
    """Render the locked safety gating rules as prompt text for the generator.

    Mirrors ``stratification.stratification_directive`` and
    ``design_decisions.design_directive``: these are settled facts the model
    narrates, never a menu it re-decides. Every numeric boundary is reproduced
    exactly, because a rounded or "approximately" version of a stopping rule is
    not a stopping rule.
    """
    if not gating or not (gating.get("individual_rules") or gating.get("cohort_rules")):
        return ""

    lines = [
        # The instruction below used to read "narrate them exactly as given", which
        # made the model reproduce every ~90-word trigger verbatim and turned the
        # safety section into several pages of near-duplicate prose. What must be
        # preserved is the SET of rules and every NUMBER in them - not the wording.
        # A stopping rule a monitor cannot find at 2am is not a safety control.
        "SAFETY GATING RULES - these are ALREADY DETERMINED. Reproduce every rule and "
        "every numeric threshold, count and timeframe EXACTLY; never invent, round, "
        "soften or omit one. But express each one CONCISELY - compress the supplied "
        "wording into the shortest form that keeps the trigger unambiguous and the "
        "action actionable. Do not copy the source phrasing verbatim, and do not pad "
        "with justification: this section is operative instruction, not discussion.",
        f"Overview: {gating.get('summary', '')}",
    ]

    grading = gating.get("grading_scale") or {}
    if grading:
        # CTCAE is a published, universally-available scale. Reproducing all five
        # full definitions is padding every reviewer skips - name the scale, give
        # the one-word grade titles, and carry only the protocol-specific
        # consequences, which is the part that is NOT in CTCAE.
        lines.append(
            f"AE SEVERITY GRADING - state in ONE sentence that adverse events are graded by "
            f"{grading.get('name', '')} ({grading.get('citation', '')}). Do NOT reproduce the "
            "full CTCAE definitions - it is a published scale and copying it wastes the "
            "reader's attention. Give a compact table of Grade | Title | Protocol "
            "consequence ONLY:"
        )
        for g in grading.get("grades", []):
            lines.append(
                f"  Grade {g['grade']} ({g['title']}) -> {g['protocol_action']}"
            )
        lines.append(
            "  Then, in no more than two sentences total, state the severity-vs-seriousness "
            f"distinction and the causality basis: {grading.get('severity_vs_seriousness', '')} "
            f"{grading.get('causality_basis', '')}"
        )

    # Rendered as TABLE ROWS, not prose, and WITHOUT the per-rule rationale.
    # The rationale explains why this engine selected the rule; it is provenance
    # for the author, not protocol content, and reproducing 19 of them turned the
    # safety section into several pages a reviewer will skim. A stopping rule is
    # operative text: the trigger and the action are what a site acts on.
    lines.append(
        "INDIVIDUAL SUBJECT STOPPING RULES - these withdraw ONE subject from further "
        "dosing and never, by themselves, stop the study. Render as a COMPACT HTML "
        "<table> with columns: Rule | Trigger | Action. One row per rule, no "
        "introductory paragraph, and do NOT add a rationale column - the trigger and "
        "the action are the operative content. Keep every numeric threshold verbatim. "
        "TARGET ONE SHORT SENTENCE PER CELL: state the threshold and what happens, and "
        "stop. Where several rules share an action (for example 'no further study drug "
        "for this subject'), state it once in the Action cell rather than re-explaining "
        "the consequence each time:"
    )
    for i, r in enumerate(gating.get("individual_rules", []), 1):
        lines.append(
            f"  I{i}. {r['label']} | TRIGGER: {r['trigger']} | ACTION: {r['action']}"
        )

    lines.append(
        "COHORT / STUDY HALTING RULES - these stop ANY further subject being dosed. "
        "Render as a SECOND compact <table> with the same three columns, kept separate "
        "from the individual rules (the distinction is what a monitor acts on). Again "
        "no rationale column and no preamble, and the same one-short-sentence-per-cell "
        "target. Do NOT restate an individual rule here in different words - if a "
        "trigger already appears in the individual table, this table states only the "
        "cohort-level count or threshold that stops the whole study:"
    )
    for i, r in enumerate(gating.get("cohort_rules", []), 1):
        lines.append(
            f"  H{i}. {r['label']} | TRIGGER: {r['trigger']} | ACTION: {r['action']}"
        )

    if gating.get("review_and_resumption"):
        lines.append(f"HALT REVIEW AND RESUMPTION: {gating['review_and_resumption']}")

    discharge = gating.get("discharge_criteria") or {}
    if discharge:
        lines.append(
            "CONFINEMENT DISCHARGE CRITERIA - a dosed subject may leave the unit only when "
            "ALL of the following are met. Present them as a numbered list and keep every "
            "numeric range exactly as written:"
        )
        for i, c in enumerate(discharge.get("criteria", []), 1):
            lines.append(f"  D{i}. {c['criterion']}")
        lines.append(f"  Authorisation: {discharge.get('authorisation', '')}")
        lines.append(f"  If criteria are not met: {discharge.get('if_not_met', '')}")
        if discharge.get("design_note"):
            lines.append(f"  Design note: {discharge['design_note']}")

    escalations = gating.get("escalations") or []
    if escalations:
        lines.append("ESCALATIONS APPLIED TO THIS STUDY (state the reason in the text):")
        for e in escalations:
            lines.append(f"  - {e['reason']} {e['effect']}")

    assumptions = gating.get("assumptions") or []
    if assumptions:
        lines.append(
            "DISCLOSED ASSUMPTIONS - these values are unit/sponsor conventions rather than "
            "regulatory requirements. State them as pre-specified protocol conventions; do "
            "NOT attribute them to a guideline:"
        )
        for a in assumptions:
            lines.append(f"  - {a['key']}: {a['value']} ({a['basis']})")

    return "\n".join(lines)
