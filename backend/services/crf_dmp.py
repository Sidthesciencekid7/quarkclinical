"""Case Report Form (CRF) specification and Data Management Plan (DMP).

WHY THIS MODULE EXISTS
----------------------
The ``data_handling`` protocol section is pure AI prose: it describes source
documents, eCRF completion and database lock in generic language. What it never
produced is the two artifacts a sponsor actually has to hand a CRO before a
single subject is dosed:

* the **CRF specification** - the list of forms, the fields on each form, the
  permitted values, and the programmed edit checks; and
* the **Data Management Plan** - how those data flow, get coded, get queried,
  get reconciled against the safety database, get locked and get archived.

Without them the protocol says data will be collected but never says WHAT. That
gap is not cosmetic. Two of its failure modes destroy a bioequivalence study
outright and cannot be repaired by re-analysis:

  1. **Only the nominal PK time is captured.** Non-compartmental analysis
     integrates concentration against the time that ELAPSED between the dose and
     the draw. If the CRF records "2 h" instead of the actual clock time of the
     draw and the actual clock time of the dose, then a sample drawn 17 minutes
     late is analysed as if it were on time. AUC and lambda_z are biased by an
     amount nobody can quantify afterwards, because the real time was never
     written down. :mod:`services.nca` therefore consumes an ELAPSED time, and
     this specification makes the two clock times mandatory and the elapsed time
     a derived field.
  2. **The dose time itself is not captured to the minute.** Every PK timepoint
     in :mod:`services.sampling_schedule` is relative to dosing. A missing or
     rounded dose time desynchronises the entire profile for that subject and
     period.

So this engine emits the collection instrument as FACTS: the forms, the fields,
the codelists, the ranges, the CDISC domain mapping and the edit checks are all
computed here from THIS study's own design and injected into the prompt as a
locked block the model may narrate but not re-invent - the same contract as
:mod:`services.stratification` (covariate balance),
:mod:`services.sampling_schedule` (timepoints) and
:mod:`services.biostatistics` (sample size).

WHAT MAKES IT THIS STUDY'S CRF AND NOT A TEMPLATE
-------------------------------------------------
The forms are instantiated against the real design, not a generic skeleton:

* the number of dosing periods, the sequence codelist and the per-period repeat
  structure come from the design (crossover/replicate repeat every dosing and PK
  form once PER PERIOD; parallel does not, and has no washout and no carry-over
  pre-dose rule);
* the PK sample log is pre-printed with the ACTUAL nominal times computed by
  :mod:`services.sampling_schedule`, each with its own permitted collection
  window, so a site cannot invent a timepoint and a monitor can see a missing
  one;
* the visit structure is built from the sampling horizon (which draws are
  in-house and which are ambulatory follow-up visits) and from the locked
  washout;
* conditional forms and fields are switched on by cues found in the REAL drug
  label text - a QT-prolonging drug gets triplicate ECGs with an ICH E14 alert
  threshold, a hepatotoxic drug gets a Hy's Law liver-event follow-up form, a
  drug with fetal-risk labelling gets a pregnancy test that hard-blocks dosing.

THE BRIDGE TO THIS PRODUCT'S OWN ANALYSIS PIPELINE
--------------------------------------------------
ProtocolAI already analyses the finished study: :mod:`services.dataset_ingest`
defines the upload contract and :mod:`services.nca` computes the exposure
metrics from it. A CRF that does not capture those columns produces a study this
product cannot analyse. The specification is therefore checked, at build time,
to be a genuine SUPERSET of ``dataset_ingest.REQUIRED_COLUMNS`` for every
dataset kind, and every column is attributed to exactly one origin: a CRF field,
a derivation, the randomisation schedule, or an external electronic data
transfer (the bioanalytical concentrations are external data - a CRF that
claimed to collect them would be a lie). ``downstream_analysis_coverage``
reports that mapping and flags anything uncovered.

SCOPE
-----
This is a protocol-package artifact, not an EDC system. It specifies what will
be collected and how the data will be managed; it does not capture data, hold a
database, transmit anything to a regulator, or run a workflow.

WHAT REACHES THE PROTOCOL, AND WHAT DOES NOT
--------------------------------------------
A protocol and a case report form specification are two different documents. A
protocol states THAT data are recorded on a case report form and managed under a
Data Management Plan; it does not reproduce the collection instrument. A
reviewing clinical pharmacologist reading a protocol should not be handed the
dosing log's critical-field list (EX), the PK blood sample collection log's
critical-field list (PC), the adverse event form's critical-field list (AE), or a
pre-printed sample-log row for every timepoint in every period - that material
belongs to the CRF specification and the Statistical Analysis Plan package.

So the ENGINE still computes all of it (``build_crf_spec`` is unchanged and the
standalone CRF/DMP export is built from it), but what the PROTOCOL prompt quotes
is controlled by the ``scope`` argument of :func:`crf_dmp_directive`, whose
default is now ``"protocol"``: a brief statement of data capture, coding, query
management, lock, audit trail and retention, plus an explicit instruction NOT to
render the field-level tables or the pre-printed sample-log rows. ``scope="crf"``
(alias ``"sap"``), ``"dmp"`` and ``"all"`` still render the full specification for
the separate document that is meant to carry it.

REGULATORY FRAMING
------------------
ICH E6(R2) GCP sections 4.9 (records), 5.5 (trial management and data handling)
and 8 (essential documents); ICH E3 for the reporting expectations the CRF has
to satisfy; ICH E2A/E2B for the safety-database reconciliation; 21 CFR Part 11
(audit trail, access control, electronic signatures) and the FDA guidance on
electronic systems and records in clinical investigations; FDA "Bioequivalence
Studies With Pharmacokinetic Endpoints" (Dec 2022) for the dosing, fasting and
emesis conventions; ICH E14 for the QTc alert thresholds; FDA drug-induced liver
injury guidance (2009) for Hy's Law; CDISC CDASH (collection), SDTM (submission
tabulation) and Define-XML (metadata); EU Regulation 536/2014 Art. 58 for the
25-year clinical trial master file retention.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

# Reused, never re-derived: the washout the protocol already locks in
# ai_service._locked_design_directive is 7 x t1/2. The CRF's between-period
# visit structure has to sit on the SAME arithmetic or the schedule of
# assessments and the study design section will disagree about study duration.
from services.biostatistics import WASHOUT_HALF_LIVES

# The downstream analysis contract. Imported (not copied) so that adding a
# required column to the upload contract immediately shows up here as an
# uncovered column instead of silently drifting.
from services.dataset_ingest import OPTIONAL_COLUMNS as _ANALYSIS_OPTIONAL_COLUMNS
from services.dataset_ingest import REQUIRED_COLUMNS as _ANALYSIS_REQUIRED_COLUMNS

# ---------------------------------------------------------------------------
# Standards versions
# ---------------------------------------------------------------------------
# Pinned so the CRF specification, the annotated CRF and the submission datasets
# all name the same version. These are the versions supported by the FDA Data
# Standards Catalog at the time of writing; the DMP states that the version in
# force at database lock governs and must be re-confirmed against the current
# catalogue before the submission datasets are built.
SDTM_IG_VERSION = "SDTMIG v3.4"
CDASH_IG_VERSION = "CDASHIG v2.2"
DEFINE_XML_VERSION = "Define-XML v2.1"
CDISC_CT_POLICY = (
    "CDISC SDTM Controlled Terminology, quarterly release; the package current at "
    "database lock governs and is recorded in the define.xml"
)

# ---------------------------------------------------------------------------
# Visit-structure assumptions
# ---------------------------------------------------------------------------
# Conventional healthy-volunteer BE screening window. 28 days is the usual
# outer bound: long enough to schedule laboratory turnaround and re-tests, short
# enough that screening haematology/chemistry still represent the subject at
# dosing. Surfaced in `assumptions` so the protocol can state it.
SCREENING_WINDOW_DAYS = 28
# Admission the evening before dosing. Set to cover the 10 h overnight fast plus
# check-in assessments (vitals, urine drug screen, pregnancy test) with margin.
CONFINEMENT_ADMISSION_HOURS_PRE_DOSE = 12.0
# Subjects stay in-house through the 24 h draw; later timepoints are ambulatory
# visits. This is the conventional split and it is what makes a long-tailed
# schedule affordable - it is an assumption, not a regulation, so it is
# disclosed.
CONFINEMENT_DISCHARGE_HOURS = 24.0
# Post-study safety follow-up contact/visit after the LAST dose.
FOLLOW_UP_DAYS_AFTER_LAST_DOSE = 7
FOLLOW_UP_WINDOW_DAYS = 2

# ---------------------------------------------------------------------------
# Dosing conventions (FDA BE guidance, Dec 2022)
# ---------------------------------------------------------------------------
OVERNIGHT_FAST_HOURS = 10.0          # "at least 10 hours" before dosing
POST_DOSE_FAST_HOURS = 4.0           # no food for 4 h after dosing
DOSING_WATER_VOLUME_ML = 240.0       # 240 mL (8 fl oz) of water with the dose
# Standardised meals are served at fixed times after dosing and must be
# IDENTICAL in composition and timing in every period - otherwise the meal
# becomes a period effect in a crossover.
STANDARD_MEAL_HOURS_POST_DOSE = (4.0, 10.0)
# Emesis rule: for an immediate-release product, data from a subject who vomits
# at or before 2 x median Tmax are excluded from the statistical analysis. The
# CRF must therefore capture the emesis time to the minute, not merely "yes".
EMESIS_EXCLUSION_TMAX_MULTIPLE = 2.0

# Food-effect meal (FDA "Food-Effect Bioavailability and Fed Bioequivalence
# Studies", Dec 2002): a high-fat, high-calorie breakfast of 800-1000 kcal with
# 500-600 kcal from fat, eaten within 30 minutes, with the dose given 30 minutes
# after the START of the meal.
HIGH_FAT_MEAL_KCAL_RANGE = (800.0, 1000.0)
HIGH_FAT_MEAL_FAT_KCAL_RANGE = (500.0, 600.0)
FOOD_EFFECT_MEAL_DURATION_MIN = 30
FOOD_EFFECT_DOSE_AFTER_MEAL_START_MIN = 30

# ---------------------------------------------------------------------------
# Safety alert thresholds used by the programmed edit checks
# ---------------------------------------------------------------------------
# ICH E14: an absolute QTc above 500 ms, or an increase from baseline above
# 60 ms, are the values of clinical concern. Fridericia's correction is used
# because Bazett over-corrects at the heart rates dosing and blood draws induce.
QTCF_ALERT_MS = 500.0
QTCF_CHANGE_FROM_BASELINE_ALERT_MS = 60.0
QTC_CORRECTION_DEFAULT = "Fridericia (QTcF)"
# Hy's Law (FDA DILI guidance, 2009): ALT or AST >= 3 x ULN together with total
# bilirubin >= 2 x ULN, in the absence of cholestasis (ALP < 2 x ULN).
HYS_LAW_TRANSAMINASE_X_ULN = 3.0
HYS_LAW_BILIRUBIN_X_ULN = 2.0
HYS_LAW_ALP_X_ULN = 2.0

# ---------------------------------------------------------------------------
# Sample handling
# ---------------------------------------------------------------------------
# Whole blood must reach the centrifuge quickly or ex vivo degradation and
# haemolysis start to move the measured concentration. 30 minutes is the usual
# protocol limit and is an assumption to be reconciled with the bioanalytical
# method's own stability data before the study starts.
MAX_TIME_TO_CENTRIFUGE_MIN = 30
PLASMA_STORAGE_TEMP_C = -70.0
# Clock times are recorded to the minute everywhere. Rounding to 5 minutes is a
# 4% timing error on an early absorption sample of a fast drug.
TIME_RECORDING_PRECISION_MIN = 1

# ---------------------------------------------------------------------------
# Data management
# ---------------------------------------------------------------------------
QUERY_RESPONSE_TARGET_DAYS = 5        # site turnaround target for an open query
SAE_RECONCILIATION_INTERVAL_DAYS = 30  # routine cadence, plus a final pass pre-lock
# EU Regulation 536/2014 Art. 58 requires the clinical trial master file to be
# retained for at least 25 years; ICH E6(R2) 5.5.11 and 21 CFR 312.62(c) set
# shorter floors, so the longest applicable period governs.
ARCHIVE_RETENTION_YEARS = 25
MEDDRA_RELEASE_CADENCE = "twice yearly (March and September)"
WHODRUG_RELEASE_CADENCE = "twice yearly (1 March and 1 September)"

# SDTM study-day (--DY) convention. There is NO Day 0: the day of first dose is
# Day 1 and the day before it is Day -1. dataset_ingest's ae.start_day is an
# integer computed this way, so the CRF must capture both the AE onset date and
# the first-dose date for it to be derivable at all.
STUDY_DAY_HAS_NO_DAY_ZERO = True


# ---------------------------------------------------------------------------
# Field / form model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CRFField:
    """One data item on one form.

    ``analysis_columns`` names the ``dataset_ingest`` column(s) this item
    ultimately supplies, in ``"<kind>.<column>"`` form. That is what turns the
    CRF from a document into a checkable contract with the analysis pipeline.
    """

    name: str                      # database item name (short, stable)
    label: str                     # the question as printed on the form
    dtype: str                     # text|integer|decimal|date|time|datetime|codelist|boolean
    units: str = ""
    codelist: tuple[str, ...] = ()
    range_low: Optional[float] = None
    range_high: Optional[float] = None
    required: bool = True
    sdtm_variable: str = ""
    analysis_columns: tuple[str, ...] = ()
    derived: bool = False          # computed by the database, not typed by the site
    derivation: str = ""           # the exact derivation, when derived
    conditional_on: str = ""       # condition key that switches this field on
    note: str = ""

    def permitted_text(self) -> str:
        """Human-readable permitted range / codelist for the printed spec."""
        if self.codelist:
            return "One of: " + " | ".join(self.codelist)
        if self.range_low is not None or self.range_high is not None:
            lo = "" if self.range_low is None else _num(self.range_low)
            hi = "" if self.range_high is None else _num(self.range_high)
            unit = f" {self.units}" if self.units else ""
            if lo and hi:
                return f"{lo} to {hi}{unit}"
            if lo:
                return f">= {lo}{unit}"
            return f"<= {hi}{unit}"
        if self.dtype == "date":
            return ("ISO 8601 date (YYYY-MM-DD); an unknown component is recorded "
                    "per the SDTM ISO 8601 partial-date convention, never guessed")
        if self.dtype == "time":
            return (f"24-hour clock HH:MM, recorded to the nearest "
                    f"{TIME_RECORDING_PRECISION_MIN} minute")
        if self.dtype == "datetime":
            return (f"ISO 8601 date and 24-hour time to the nearest "
                    f"{TIME_RECORDING_PRECISION_MIN} minute")
        if self.dtype == "boolean":
            return "Yes | No"
        if self.dtype in ("integer", "decimal"):
            return f"Numeric{(' (' + self.units + ')') if self.units else ''}"
        return "Free text"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Tuples would survive json.dumps but come back as lists, so the result
        # would not compare equal after a round-trip. Normalise here.
        d["codelist"] = list(self.codelist)
        d["analysis_columns"] = list(self.analysis_columns)
        d["permitted"] = self.permitted_text()
        return d


@dataclass(frozen=True)
class CRFForm:
    """One CRF form (an eCRF page / CDASH domain-level collection instrument)."""

    key: str
    title: str
    sdtm_domain: str
    purpose: str
    completed_at: str              # when in the schedule it is completed
    repeating: str                 # once|per_period|per_visit|per_record|per_sample
    fields: tuple[CRFField, ...]
    conditional_on: str = ""       # condition key that switches the whole form on
    rationale: str = ""            # why this form exists at all
    critical: bool = False         # a form whose loss cannot be repaired later

    def to_dict(self, active: Optional[set] = None) -> dict[str, Any]:
        fields = [
            f.to_dict() for f in self.fields
            if not f.conditional_on or (active is not None and f.conditional_on in active)
        ]
        return {
            "key": self.key,
            "title": self.title,
            "sdtm_domain": self.sdtm_domain,
            "purpose": self.purpose,
            "completed_at": self.completed_at,
            "repeating": self.repeating,
            "conditional_on": self.conditional_on,
            "rationale": self.rationale,
            "critical": self.critical,
            "n_fields": len(fields),
            "fields": fields,
        }


@dataclass(frozen=True)
class EditCheck:
    """One programmed validation rule.

    ``severity``:
      * ``hard``   - the entry is rejected; the value cannot be saved as typed.
      * ``query``  - the value is saved and an automatic query fires to the site.
      * ``review`` - no automatic query; the record is routed for medical or data
        review (used where the correct action needs judgement, not arithmetic).
    """

    key: str
    check_type: str                # range|cross_form|missing|consistency|uniqueness|reconciliation
    severity: str
    forms: tuple[str, ...]
    fields: tuple[str, ...]
    rule: str                      # the programmable statement
    rationale: str                 # what clinical error it prevents
    action: str
    conditional_on: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["forms"] = list(self.forms)
        d["fields"] = list(self.fields)
        return d


@dataclass(frozen=True)
class DMPSection:
    """One section of the Data Management Plan."""

    key: str
    title: str
    content: str
    deliverables: tuple[str, ...]
    responsible: str
    timing: str
    reference: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["deliverables"] = list(self.deliverables)
        return d


@dataclass(frozen=True)
class AnalysisColumnOrigin:
    """Where a downstream analysis column comes from when it is NOT a CRF field.

    Concentrations are the important case: they are produced by the bioanalytical
    laboratory and arrive as an external electronic transfer. A CRF that claimed
    to collect them would be describing a process that does not exist.
    """

    column: str                    # "<kind>.<column>"
    source: str                    # external|derived|randomization
    origin: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num(x: float) -> str:
    """Compact numeric rendering for printed ranges (3 -> '3', 18.5 -> '18.5')."""
    if x == int(x):
        return str(int(x))
    return f"{x:g}"


def _f(name: str, label: str, dtype: str, **kw: Any) -> CRFField:
    return CRFField(name=name, label=label, dtype=dtype, **kw)


# ---------------------------------------------------------------------------
# Codelists reused across forms
# ---------------------------------------------------------------------------
_YES_NO = ("Yes", "No")
_SEVERITY = ("Mild", "Moderate", "Severe")
# Five-point causality scale. The downstream ``ae.related`` column is a boolean,
# so the collapse rule has to be pre-specified rather than decided at analysis:
# Possible / Probable / Definite are counted as related.
_CAUSALITY = ("Not related", "Unlikely related", "Possibly related",
              "Probably related", "Definitely related")
_RELATED_TRUE_VALUES = ("Possibly related", "Probably related", "Definitely related")
_AE_OUTCOME = ("Recovered/Resolved", "Recovering/Resolving",
               "Not recovered/Not resolved", "Recovered with sequelae",
               "Fatal", "Unknown")
_SERIOUSNESS = ("Death", "Life-threatening", "Inpatient hospitalisation or "
                "prolongation of existing hospitalisation",
                "Persistent or significant disability/incapacity",
                "Congenital anomaly/birth defect",
                "Other medically important event")
_AE_ACTION = ("None", "Study drug withdrawn", "Dose not changed",
              "Concomitant medication given", "Non-drug therapy given",
              "Subject hospitalised", "Not applicable")
_CLIN_SIG = ("Not clinically significant", "Clinically significant")
_MATRIX = ("K2EDTA plasma", "K3EDTA plasma", "Sodium heparin plasma", "Serum")
_HAEMOLYSIS = ("None", "Slight", "Moderate", "Gross")
_PK_DEVIATION_REASON = (
    "Within window - no deviation", "Difficult venous access",
    "Site logistics/staffing", "Subject unavailable or refused",
    "Adverse event or medical intervention", "Cannula blocked or replaced",
    "Sample lost, spilled or damaged", "Other (specify)",
)
_NOT_COLLECTED_REASON = (
    "Subject withdrew", "Venous access failed", "Adverse event",
    "Subject refused", "Sample lost or damaged in processing",
    "Site error - draw missed", "Other (specify)",
)
_DISCONT_REASON = (
    "Completed as planned", "Adverse event", "Withdrawal by subject",
    "Lost to follow-up", "Protocol deviation", "Physician decision",
    "Positive drug or alcohol screen", "Emesis within the exclusion window",
    "Failure to meet continuation criteria", "Sponsor termination of study",
    "Other (specify)",
)


# ---------------------------------------------------------------------------
# Form catalogue
# ---------------------------------------------------------------------------
# Ordered as the casebook is ordered: administrative and screening forms first,
# then the per-period treatment and PK forms, then safety, then disposition.
# `critical=True` marks a form whose data cannot be reconstructed after the fact
# - the dosing log and the PK sample log both carry a clock time that exists
# nowhere else once the subject has gone home.

_SUBJECT_ID_FIELD = _f(
    "subject_id", "Subject identifier", "text",
    sdtm_variable="DM.USUBJID",
    analysis_columns=("nca.subject", "params.subject", "ae.subject"),
    note=("Unique across the whole study and never reused, including for a "
          "re-screened or replacement subject. It is the join key for every "
          "form, for the bioanalytical transfer and for the safety database."),
)

_CATALOGUE: tuple[CRFForm, ...] = (
    # -- Administrative / screening ----------------------------------------
    CRFForm(
        key="dm_demographics",
        title="Subject identification and demographics",
        sdtm_domain="DM",
        purpose=("Identifies the subject, records consent, and captures the "
                 "baseline characteristics that the randomisation is stratified "
                 "on and that the PK summary is presented by."),
        completed_at="Screening (once per subject)",
        repeating="once",
        rationale=("Demographic covariates are the denominator of every PK "
                   "summary and the basis of the stratified randomisation; "
                   "sex and body size in particular drive dose-normalised "
                   "exposure at a fixed dose."),
        fields=(
            _SUBJECT_ID_FIELD,
            _f("site_id", "Site identifier", "text", sdtm_variable="DM.SITEID"),
            _f("screening_number", "Screening number", "text",
               note="Assigned before eligibility is known; retained for the "
                    "screening-failure log and the CONSORT-style accounting."),
            _f("consent_date", "Date informed consent signed", "date",
               sdtm_variable="DM.RFICDTC",
               note="Must precede EVERY study-specific procedure, including "
                    "screening blood draws (ICH E6(R2) 4.8.8)."),
            _f("consent_time", "Time informed consent signed", "time",
               note="Required so that a same-day screening procedure can be "
                    "shown to have followed consent, not preceded it."),
            _f("consent_version", "Version/date of the consent form signed", "text",
               note="A subject re-consented after an amendment must have the "
                    "later version recorded."),
            _f("birth_date", "Date of birth", "date", required=False,
               sdtm_variable="DM.BRTHDTC",
               note="Collected only where local privacy law permits; otherwise "
                    "age alone is recorded."),
            _f("age", "Age at consent", "integer", units="years",
               range_low=18, range_high=55, sdtm_variable="DM.AGE",
               note="Range mirrors the conventional healthy-volunteer inclusion "
                    "criterion; a value outside it is a hard stop, not a query."),
            _f("sex", "Sex", "codelist", codelist=("Male", "Female"),
               sdtm_variable="DM.SEX"),
            _f("childbearing_potential", "Woman of childbearing potential", "codelist",
               codelist=("Yes", "No - surgically sterile",
                         "No - postmenopausal >= 12 months", "Not applicable (male)"),
               note="Drives the pregnancy-test and contraception requirements."),
            _f("race", "Race", "codelist",
               codelist=("American Indian or Alaska Native", "Asian",
                         "Black or African American",
                         "Native Hawaiian or Other Pacific Islander", "White",
                         "Multiple", "Not reported", "Unknown"),
               sdtm_variable="DM.RACE",
               note="CDISC controlled terminology; multiple selection permitted "
                    "and mapped to SUPPDM when more than one is chosen."),
            _f("ethnicity", "Ethnicity", "codelist",
               codelist=("Hispanic or Latino", "Not Hispanic or Latino",
                         "Not reported", "Unknown"),
               sdtm_variable="DM.ETHNIC"),
            _f("height_cm", "Height", "decimal", units="cm",
               range_low=140.0, range_high=210.0, sdtm_variable="VS.HEIGHT"),
            _f("weight_kg", "Body weight", "decimal", units="kg",
               range_low=40.0, range_high=130.0, sdtm_variable="VS.WEIGHT"),
            _f("bmi", "Body mass index", "decimal", units="kg/m2",
               range_low=18.5, range_high=30.0, derived=True,
               derivation="weight_kg / (height_cm / 100)^2, rounded to 1 decimal",
               note="Derived rather than typed: a hand-computed BMI is one of "
                    "the most common eligibility errors, and the entry range is "
                    "the inclusion criterion itself."),
            _f("randomization_number", "Randomisation number", "text",
               required=False,
               note="Assigned at Period 1 Day -1 from the randomisation "
                    "schedule; blank for a screen failure."),
            _f("sequence", "Treatment sequence", "codelist",
               # Catalogue default is the standard 2x2x2 crossover; the real
               # codelist is substituted from THIS study's design before the
               # form is emitted.
               codelist=("TR", "RT"),
               sdtm_variable="DM.ARM",
               analysis_columns=("nca.sequence", "params.sequence"),
               note="Codelist is generated from the design; the value is "
                    "populated from the randomisation schedule, never typed by "
                    "the site."),
        ),
    ),
    CRFForm(
        key="ie_eligibility",
        title="Eligibility - inclusion and exclusion criteria",
        sdtm_domain="IE",
        purpose=("Records confirmation that every inclusion criterion is met and "
                 "no exclusion criterion applies, at screening and again "
                 "immediately before the first dose."),
        completed_at="Screening and re-confirmed at each period admission (Day -1)",
        repeating="per_visit",
        rationale=("Enrolling a subject who did not meet the criteria is the "
                   "single most common major protocol deviation and it "
                   "propagates into the analysis population."),
        fields=(
            _f("assessment_date", "Date of eligibility assessment", "date"),
            _f("all_inclusion_met", "All inclusion criteria met", "boolean",
               sdtm_variable="IE.IETESTCD"),
            _f("inclusion_not_met", "Inclusion criteria NOT met (numbers)", "text",
               required=False,
               note="Required whenever all_inclusion_met = No. Recorded by "
                    "criterion number so the screening-failure reasons tabulate."),
            _f("all_exclusion_absent", "No exclusion criterion applies", "boolean"),
            _f("exclusion_met", "Exclusion criteria met (numbers)", "text",
               required=False),
            _f("eligible", "Subject eligible for enrolment", "boolean",
               derived=True,
               derivation="all_inclusion_met = Yes AND all_exclusion_absent = Yes"),
            _f("rescreened", "Subject previously screened for this study", "boolean",
               note="A re-screened subject keeps a NEW subject identifier and "
                    "the link is recorded here, so a subject is never counted "
                    "twice in the disposition table."),
            _f("waiver_requested", "Any protocol waiver requested for this subject",
               "boolean",
               note=("Must be No. Prospective waivers of protocol requirements "
                     "are not permitted (ICH E6(R2) 5.20.1); a departure that "
                     "has already happened is recorded as a protocol deviation, "
                     "not authorised in advance.")),
            _f("assessed_by", "Assessed by (investigator or delegate)", "text"),
        ),
    ),
    CRFForm(
        key="mh_medical_history",
        title="Medical history and current medical conditions",
        sdtm_domain="MH",
        purpose=("Captures past and ongoing conditions, so that a pre-existing "
                 "condition is not later reported as an adverse event and so "
                 "exclusionary conditions are visible."),
        completed_at="Screening; one record per condition",
        repeating="per_record",
        rationale=("The distinction between a pre-existing condition and a "
                   "treatment-emergent adverse event is decided by whether the "
                   "condition is on this form. Coding both to MedDRA is what "
                   "makes that check programmable."),
        fields=(
            _f("mh_term", "Condition (verbatim as reported)", "text",
               sdtm_variable="MH.MHTERM",
               note="Recorded verbatim; never pre-coded by the site."),
            _f("mh_meddra_llt", "MedDRA Lowest Level Term", "text", derived=True,
               derivation="Assigned by central medical coding, not by the site",
               sdtm_variable="MH.MHLLT"),
            _f("mh_meddra_pt", "MedDRA Preferred Term", "text", derived=True,
               derivation="Assigned by central medical coding", sdtm_variable="MH.MHDECOD"),
            _f("mh_meddra_soc", "MedDRA System Organ Class", "text", derived=True,
               derivation="Assigned by central medical coding", sdtm_variable="MH.MHBODSYS"),
            _f("start_date", "Start date", "date", required=False,
               sdtm_variable="MH.MHSTDTC",
               note="Partial dates are permitted and recorded as such; an "
                    "invented day is worse than a missing one."),
            _f("ongoing", "Condition ongoing at screening", "boolean",
               sdtm_variable="MH.MHENRF"),
            _f("end_date", "End date", "date", required=False,
               sdtm_variable="MH.MHENDTC",
               note="Required when ongoing = No."),
            _f("exclusionary", "Condition is exclusionary", "boolean",
               note="Cross-checked against the eligibility form."),
        ),
    ),
    CRFForm(
        key="cm_concomitant_medications",
        title="Prior and concomitant medications",
        sdtm_domain="CM",
        purpose=("Records every medication, vitamin, herbal and over-the-counter "
                 "product taken from the protocol-defined lookback through the "
                 "end of study."),
        completed_at="Screening through the follow-up visit; one record per medication",
        repeating="per_record",
        rationale=("A concomitant inhibitor or inducer of the drug's clearance "
                   "pathway changes exposure by more than the 20% the study is "
                   "trying to detect. WHODrug coding is what turns free-text "
                   "medication names into an interrogable interaction check."),
        fields=(
            _f("cm_trt", "Medication (trade or generic name, verbatim)", "text",
               sdtm_variable="CM.CMTRT"),
            _f("whodrug_code", "WHODrug Drug Record Number", "text", derived=True,
               derivation="Assigned by central medical coding against the "
                          "WHODrug Global version in force",
               sdtm_variable="CM.CMDECOD"),
            _f("atc_class", "WHODrug ATC class", "text", derived=True,
               derivation="Taken from the WHODrug entry at coding",
               sdtm_variable="CM.CMCLAS"),
            _f("indication", "Indication / reason for use", "text",
               sdtm_variable="CM.CMINDC"),
            _f("dose", "Dose", "decimal", required=False, sdtm_variable="CM.CMDOSE"),
            _f("dose_unit", "Dose unit", "text", required=False,
               sdtm_variable="CM.CMDOSU"),
            _f("frequency", "Frequency", "text", required=False,
               sdtm_variable="CM.CMDOSFRQ"),
            _f("route", "Route", "text", required=False, sdtm_variable="CM.CMROUTE"),
            _f("start_date", "Start date", "date", sdtm_variable="CM.CMSTDTC"),
            _f("ongoing", "Ongoing", "boolean", sdtm_variable="CM.CMENRF"),
            _f("end_date", "End date", "date", required=False,
               sdtm_variable="CM.CMENDTC"),
            _f("prior_or_concomitant", "Prior or concomitant", "codelist",
               codelist=("Prior (stopped before first dose)",
                         "Concomitant (taken on or after first dose)"),
               derived=True,
               derivation="start_date/end_date compared with the first-dose date"),
            _f("prohibited", "Prohibited by the protocol", "boolean",
               note="Flags a medication on the prohibited list; triggers a "
                    "protocol-deviation record and a data-review decision on the "
                    "subject's evaluability."),
            _f("given_for_ae", "Given to treat an adverse event", "boolean"),
            _f("ae_number", "Related adverse event number", "text", required=False,
               note="Required when given_for_ae = Yes; links CM to AE."),
        ),
    ),
    # -- Treatment ----------------------------------------------------------
    CRFForm(
        key="ex_dosing",
        title="Study drug administration (dosing log)",
        sdtm_domain="EX",
        purpose=("Records exactly what was administered, and exactly WHEN. The "
                 "dose date and time are the time origin of the entire PK "
                 "profile for that subject and period."),
        completed_at="Each dosing day, once per period",
        repeating="per_period",
        critical=True,
        rationale=("Every sampling time in this protocol is expressed relative "
                   "to dosing. If the dose clock time is missing or rounded, "
                   "every elapsed time in that period is wrong by the same "
                   "unknown amount, and no re-analysis can recover it."),
        fields=(
            _f("period", "Period number", "integer", range_low=1,
               sdtm_variable="EX.VISITNUM",
               analysis_columns=("nca.period", "params.period")),
            _f("treatment", "Treatment administered", "codelist",
               codelist=("T (Test)", "R (Reference)"),
               sdtm_variable="EX.EXTRT",
               analysis_columns=("nca.treatment", "params.treatment"),
               note="Populated from the randomisation schedule for the "
                    "subject's sequence and this period; verified against the "
                    "dispensing record at data review."),
            _f("product_name", "Product name and manufacturer", "text",
               sdtm_variable="EX.EXTRT"),
            _f("dose_amount", "Dose administered", "decimal",
               sdtm_variable="EX.EXDOSE", range_low=0.0),
            _f("dose_unit", "Dose unit", "text", sdtm_variable="EX.EXDOSU"),
            _f("route", "Route of administration", "text",
               sdtm_variable="EX.EXROUTE"),
            _f("lot_number", "Batch/lot number", "text",
               note="Required for both products. The test batch identity is a "
                    "regulatory requirement of the bioequivalence submission "
                    "(batch size and its relationship to production scale)."),
            _f("expiry_date", "Expiry/retest date", "date",
               note="Checked to be after the dosing date - a hard check."),
            _f("dose_date", "Date of administration", "date",
               sdtm_variable="EX.EXSTDTC"),
            _f("dose_time", "CLOCK TIME of administration", "time",
               sdtm_variable="EX.EXSTDTC",
               note=(f"Recorded to the nearest {TIME_RECORDING_PRECISION_MIN} "
                     f"minute from the same synchronised site clock used for the "
                     f"blood draws. This is the time origin for every PK "
                     f"timepoint; it may not be back-filled from memory.")),
            _f("fast_start_datetime", "Start of the overnight fast", "datetime",
               note=f"Fast of at least {_num(OVERNIGHT_FAST_HOURS)} h before "
                    f"dosing (FDA BE guidance)."),
            _f("fast_duration_h", "Fast duration before dosing", "decimal",
               units="h", derived=True,
               derivation="dose_datetime - fast_start_datetime, in hours to 1 dp",
               range_low=OVERNIGHT_FAST_HOURS),
            _f("water_volume_ml", "Volume of water given with the dose", "decimal",
               units="mL", range_low=DOSING_WATER_VOLUME_ML,
               range_high=DOSING_WATER_VOLUME_ML,
               note=f"Fixed at {_num(DOSING_WATER_VOLUME_ML)} mL in every period "
                    f"(FDA BE guidance); a different volume is a deviation, not "
                    f"a free choice, because gastric volume changes dissolution."),
            _f("dose_administered_completely", "Full dose administered", "boolean"),
            _f("partial_dose_reason", "Reason the full dose was not administered",
               "text", required=False),
            _f("mouth_check_performed", "Mouth check performed after dosing",
               "boolean",
               note="Confirms ingestion; the standard compliance control for an "
                    "oral dose in a confined study."),
            _f("administered_by", "Administered by", "text"),
            _f("witnessed_by", "Witnessed/verified by", "text",
               note="Two-person verification of subject identity, product and "
                    "period assignment before administration."),
            _f("emesis_occurred", "Vomiting occurred after dosing", "boolean"),
            _f("emesis_datetime", "Date and time vomiting occurred", "datetime",
               required=False,
               note=(f"Required when emesis_occurred = Yes. Data from a subject "
                     f"who vomits at or before "
                     f"{_num(EMESIS_EXCLUSION_TMAX_MULTIPLE)} x median Tmax are "
                     f"excluded from the statistical analysis for an "
                     f"immediate-release product, so the TIME decides "
                     f"evaluability - 'Yes' alone is not analysable.")),
            _f("emesis_elapsed_h", "Time from dose to vomiting", "decimal",
               units="h", required=False, derived=True,
               derivation="emesis_datetime - dose_datetime, in hours to 2 dp"),
            _f("meal_start_time", "Start time of the standardised meal", "time",
               conditional_on="food_effect",
               note=(f"High-fat, high-calorie breakfast of "
                     f"{_num(HIGH_FAT_MEAL_KCAL_RANGE[0])}-"
                     f"{_num(HIGH_FAT_MEAL_KCAL_RANGE[1])} kcal, of which "
                     f"{_num(HIGH_FAT_MEAL_FAT_KCAL_RANGE[0])}-"
                     f"{_num(HIGH_FAT_MEAL_FAT_KCAL_RANGE[1])} kcal from fat.")),
            _f("meal_end_time", "End time of the standardised meal", "time",
               conditional_on="food_effect",
               note=f"The meal must be consumed within "
                    f"{FOOD_EFFECT_MEAL_DURATION_MIN} minutes."),
            _f("meal_percent_consumed", "Percentage of the meal consumed", "integer",
               units="%", range_low=0, range_high=100, conditional_on="food_effect",
               note="Must be 100%. Anything less makes the fed condition "
                    "undefined for that subject and period."),
            _f("injection_site", "Injection site", "codelist",
               codelist=("Abdomen - left", "Abdomen - right", "Thigh - left",
                         "Thigh - right", "Upper arm - left", "Upper arm - right"),
               conditional_on="injection_site",
               note="Absorption from a subcutaneous injection is site-dependent, "
                    "so the site is standardised and recorded, not left to the "
                    "nurse on the day."),
            _f("injection_device", "Device used", "codelist",
               codelist=("Prefilled syringe", "Autoinjector", "Vial and syringe"),
               conditional_on="injection_site"),
        ),
    ),
    CRFForm(
        key="da_drug_accountability",
        title="Study drug accountability and dispensing",
        sdtm_domain="DA",
        purpose=("Tracks receipt, dispensing, return and destruction of test and "
                 "reference product at unit level."),
        completed_at="On receipt, at each dispensing, and at study close-out",
        repeating="per_record",
        rationale=("Accountability is what allows the dosing record to be "
                   "verified independently of the CRF entry; it is a standard "
                   "inspection focus."),
        fields=(
            _f("product", "Product", "codelist",
               codelist=("T (Test)", "R (Reference)")),
            _f("lot_number", "Batch/lot number", "text"),
            _f("quantity_received", "Quantity received", "integer", range_low=0),
            _f("quantity_dispensed", "Quantity dispensed", "integer", range_low=0),
            _f("quantity_returned", "Quantity returned/unused", "integer",
               range_low=0),
            _f("quantity_destroyed", "Quantity destroyed", "integer", range_low=0,
               required=False),
            _f("reconciled", "Accountability reconciled", "boolean", derived=True,
               derivation="quantity_received = quantity_dispensed + "
                          "quantity_returned + quantity_destroyed"),
            _f("storage_temp_log_reviewed", "Storage temperature log reviewed",
               "boolean"),
            _f("dea_schedule", "DEA/controlled substance schedule", "text",
               conditional_on="controlled_substance"),
            _f("safe_log_signed", "Controlled-substance safe log signed by two "
               "authorised persons", "boolean",
               conditional_on="controlled_substance",
               note="A controlled substance requires witnessed, double-signed "
                    "custody at every transfer."),
        ),
    ),
    # -- PK -----------------------------------------------------------------
    CRFForm(
        key="pc_pk_sample_log",
        title="Pharmacokinetic blood sample collection log",
        sdtm_domain="PC",
        purpose=("Records, for every scheduled PK sample, the nominal timepoint, "
                 "the ACTUAL clock time of the draw, the deviation from the "
                 "nominal time and its reason, and the sample handling chain."),
        completed_at="Every scheduled PK timepoint, in every period",
        repeating="per_sample",
        critical=True,
        rationale=("Non-compartmental analysis integrates concentration against "
                   "ELAPSED time. A log that records only the nominal time "
                   "silently assumes every draw was on time; the resulting AUC "
                   "and lambda_z errors are undetectable and unrecoverable. "
                   "This form is what makes the study analysable."),
        fields=(
            _f("period", "Period number", "integer", range_low=1,
               analysis_columns=("nca.period",)),
            _f("nominal_time_h", "Nominal timepoint", "decimal", units="h",
               sdtm_variable="PC.PCTPTNUM",
               note="Pre-printed from the locked sampling schedule; the site "
                    "selects a row, it does not type a time. This is what stops "
                    "a site inventing or omitting a timepoint."),
            _f("nominal_time_label", "Nominal timepoint label", "text",
               derived=True, derivation="From the locked sampling schedule",
               sdtm_variable="PC.PCTPT"),
            _f("scheduled_datetime", "Scheduled clock time of the draw", "datetime",
               derived=True,
               derivation="ex_dosing.dose_datetime + nominal_time_h",
               note="Computed from the ACTUAL dose time, so the site is given "
                    "the correct target clock time even when dosing ran late."),
            _f("sample_collected", "Sample collected", "boolean"),
            _f("not_collected_reason", "Reason the sample was not collected",
               "codelist", codelist=_NOT_COLLECTED_REASON, required=False,
               note="Required when sample_collected = No. A blank here becomes "
                    "an unexplained missing concentration at analysis."),
            _f("actual_draw_date", "ACTUAL date of the draw", "date",
               sdtm_variable="PC.PCDTC"),
            _f("actual_draw_time", "ACTUAL clock time of the draw", "time",
               sdtm_variable="PC.PCDTC",
               note=(f"Recorded to the nearest {TIME_RECORDING_PRECISION_MIN} "
                     f"minute from the same synchronised clock as the dose time. "
                     f"Never back-filled with the nominal time.")),
            _f("elapsed_time_h", "Elapsed time since dosing", "decimal", units="h",
               derived=True,
               derivation=("(actual_draw_date + actual_draw_time) - "
                           "(ex_dosing.dose_date + ex_dosing.dose_time), "
                           "expressed in hours to 3 decimals"),
               analysis_columns=("nca.time",),
               note="THIS is the time used by the non-compartmental analysis, "
                    "not the nominal time. Both are retained and both are "
                    "reported."),
            _f("deviation_minutes", "Deviation from the nominal time", "decimal",
               units="min", derived=True,
               derivation="(elapsed_time_h - nominal_time_h) x 60, signed"),
            _f("within_window", "Draw within the permitted window", "boolean",
               derived=True,
               derivation="|deviation_minutes| <= the permitted window for that "
                          "timepoint, as fixed by the sampling schedule"),
            _f("deviation_reason", "Reason for deviation", "codelist",
               codelist=_PK_DEVIATION_REASON, required=False,
               note="Required whenever within_window = No."),
            _f("sample_id", "Sample identifier / barcode", "text",
               note="Unique across the study. It is the only join key between "
                    "this form and the bioanalytical concentration file, so a "
                    "duplicate makes two results unassignable."),
            _f("matrix", "Matrix and anticoagulant", "codelist", codelist=_MATRIX,
               sdtm_variable="PC.PCSPEC"),
            _f("volume_drawn_ml", "Volume drawn", "decimal", units="mL",
               range_low=0.0, range_high=20.0,
               note="Summed per subject against the protocol's total blood "
                    "volume limit."),
            _f("haemolysis", "Visible haemolysis", "codelist", codelist=_HAEMOLYSIS,
               note="Haemolysis can alter measured concentration for some "
                    "analytes; it must be visible to the bioanalytical review, "
                    "not discovered later."),
            _f("centrifuge_datetime", "Time placed in the centrifuge", "datetime"),
            _f("time_to_centrifuge_min", "Time from draw to centrifugation",
               "decimal", units="min", derived=True,
               derivation="centrifuge_datetime - actual draw datetime, in minutes",
               range_high=float(MAX_TIME_TO_CENTRIFUGE_MIN)),
            _f("aliquots_prepared", "Number of aliquots prepared", "integer",
               range_low=1, range_high=4),
            _f("freezer_datetime", "Time placed in the freezer", "datetime"),
            _f("storage_temp_c", "Storage temperature", "decimal", units="degC",
               range_high=PLASMA_STORAGE_TEMP_C,
               note=f"Nominal {_num(PLASMA_STORAGE_TEMP_C)} degC; the freezer "
                    f"temperature log is retained as source data."),
            _f("shipment_id", "Shipment identifier to the bioanalytical laboratory",
               "text", required=False),
        ),
    ),
    CRFForm(
        key="is_immunogenicity_log",
        title="Immunogenicity (ADA/NAb) sample collection log",
        sdtm_domain="IS",
        purpose=("Records the immunogenicity sample collections and links them "
                 "to the tiered anti-drug antibody assay results."),
        completed_at="Each immunogenicity timepoint",
        repeating="per_sample",
        conditional_on="immunogenicity",
        rationale=("Treatment-emergent anti-drug antibodies change clearance and "
                   "therefore the exposure comparison itself; a positive ADA "
                   "result has to be attributable to a dated pre-dose baseline "
                   "to be interpretable."),
        fields=(
            _f("period", "Period number", "integer", range_low=1, required=False),
            _f("nominal_day", "Nominal study day", "integer"),
            _f("sample_collected", "Sample collected", "boolean"),
            _f("actual_datetime", "Actual date and time of collection", "datetime"),
            _f("sample_id", "Sample identifier / barcode", "text"),
            _f("matrix", "Matrix", "codelist", codelist=("Serum",)),
            _f("baseline_sample", "Pre-dose baseline sample", "boolean",
               derived=True, derivation="nominal_day <= 1 and pre-dose",
               note="Treatment-emergent ADA cannot be classified without a "
                    "confirmed baseline result."),
        ),
    ),
    # -- Safety -------------------------------------------------------------
    CRFForm(
        key="vs_vital_signs",
        title="Vital signs",
        sdtm_domain="VS",
        purpose="Records blood pressure, pulse, respiration and temperature.",
        completed_at=("Screening, each admission, pre-dose, protocol-specified "
                      "post-dose times, discharge and follow-up"),
        repeating="per_visit",
        rationale=("Vital signs are the continuously available safety signal in "
                   "a confined healthy-volunteer study and the usual first "
                   "evidence of a haemodynamic drug effect."),
        fields=(
            _f("assessment_datetime", "Date and time of assessment", "datetime",
               sdtm_variable="VS.VSDTC"),
            _f("period", "Period number", "integer", range_low=1, required=False),
            _f("timepoint", "Nominal timepoint", "text", required=False,
               sdtm_variable="VS.VSTPT"),
            _f("position", "Position", "codelist",
               codelist=("Supine", "Sitting", "Standing"),
               sdtm_variable="VS.VSPOS",
               note="Measured after at least 5 minutes at rest, in the SAME "
                    "position at every timepoint - a posture change alone moves "
                    "blood pressure more than most drug effects in this setting."),
            _f("systolic_bp", "Systolic blood pressure", "integer", units="mmHg",
               range_low=70, range_high=200, sdtm_variable="VS.SYSBP"),
            _f("diastolic_bp", "Diastolic blood pressure", "integer", units="mmHg",
               range_low=40, range_high=120, sdtm_variable="VS.DIABP"),
            _f("pulse", "Pulse rate", "integer", units="beats/min",
               range_low=30, range_high=150, sdtm_variable="VS.PULSE"),
            _f("respiratory_rate", "Respiratory rate", "integer", units="breaths/min",
               range_low=6, range_high=40, sdtm_variable="VS.RESP"),
            _f("temperature", "Body temperature", "decimal", units="degC",
               range_low=34.0, range_high=40.0, sdtm_variable="VS.TEMP"),
            _f("temperature_location", "Temperature site", "codelist",
               codelist=("Oral", "Tympanic", "Axillary", "Temporal"),
               sdtm_variable="VS.VSLOC"),
            _f("clinical_significance", "Investigator assessment", "codelist",
               codelist=_CLIN_SIG,
               note="A clinically significant abnormality must have a "
                    "corresponding adverse event record."),
        ),
    ),
    CRFForm(
        key="eg_ecg",
        title="12-lead electrocardiogram",
        sdtm_domain="EG",
        purpose=("Records the 12-lead ECG intervals and the investigator's "
                 "overall interpretation."),
        completed_at="Screening, pre-dose, protocol-specified post-dose times, "
                     "end of study",
        repeating="per_visit",
        rationale=("QT prolongation is the cardiac safety endpoint that stops "
                   "development programmes; it is measured, not inferred."),
        fields=(
            _f("ecg_datetime", "Date and time of the ECG", "datetime",
               sdtm_variable="EG.EGDTC"),
            _f("period", "Period number", "integer", range_low=1, required=False),
            _f("timepoint", "Nominal timepoint", "text", required=False,
               sdtm_variable="EG.EGTPT"),
            _f("replicate_number", "Replicate number", "integer",
               range_low=1, range_high=3, conditional_on="qt_prolongation",
               note="Triplicate ECGs approximately 1 minute apart, averaged, "
                    "because beat-to-beat QT variability exceeds the effect size "
                    "of interest for a QT-prolonging drug."),
            _f("heart_rate", "Heart rate", "integer", units="beats/min",
               range_low=30, range_high=150, sdtm_variable="EG.EGORRES"),
            _f("pr_interval", "PR interval", "integer", units="ms",
               range_low=80, range_high=300),
            _f("qrs_duration", "QRS duration", "integer", units="ms",
               range_low=50, range_high=200),
            _f("qt_interval", "QT interval", "integer", units="ms",
               range_low=250, range_high=600),
            _f("qtcf", "QTcF (Fridericia-corrected QT)", "decimal", units="ms",
               range_low=280.0, range_high=600.0, derived=True,
               derivation="QT / (RR^(1/3)), with RR in seconds",
               note=(f"{QTC_CORRECTION_DEFAULT} is the pre-specified correction. "
                     f"Bazett over-corrects at the heart rates that dosing and "
                     f"repeated venepuncture induce, and would generate spurious "
                     f"alerts.")),
            _f("qtcf_change_from_baseline", "Change in QTcF from the period "
               "baseline", "decimal", units="ms", derived=True,
               derivation="qtcf - the pre-dose qtcf for the same subject and period"),
            _f("interpretation", "Overall interpretation", "codelist",
               codelist=("Normal", "Abnormal - not clinically significant",
                         "Abnormal - clinically significant"),
               sdtm_variable="EG.EGSTRESC"),
            _f("abnormality_description", "Description of the abnormality", "text",
               required=False),
            _f("read_by", "Read by", "codelist",
               codelist=("Investigator", "Central cardiologist"),
               note="If a central reader is used the central read is the "
                    "reportable result and arrives as an external transfer."),
        ),
    ),
    CRFForm(
        key="lb_laboratory",
        title="Clinical laboratory assessments",
        sdtm_domain="LB",
        purpose=("Records the collection event and the investigator's clinical "
                 "significance assessment for haematology, chemistry, "
                 "urinalysis, serology, drug/alcohol screen and pregnancy tests."),
        completed_at="Screening, each admission, end of study (and on repeat)",
        repeating="per_visit",
        rationale=("Laboratory results are the objective half of the safety "
                   "dataset. The RESULTS arrive electronically from the "
                   "laboratory; what the CRF has to capture is the event, the "
                   "conditions (fasting, timing) and the clinical judgement, "
                   "because none of those exist in the laboratory file."),
        fields=(
            _f("collection_datetime", "Date and time of collection", "datetime",
               sdtm_variable="LB.LBDTC"),
            _f("period", "Period number", "integer", range_low=1, required=False),
            _f("panel", "Panel", "codelist",
               codelist=("Haematology", "Serum chemistry", "Urinalysis",
                         "Serology (HBsAg, anti-HCV, HIV)",
                         "Serum pregnancy test", "Urine pregnancy test",
                         "Urine drug screen", "Alcohol breath test",
                         "Cotinine/nicotine screen", "Coagulation"),
               sdtm_variable="LB.LBCAT"),
            _f("fasting", "Sample taken fasting", "boolean",
               sdtm_variable="LB.LBFAST"),
            _f("sample_collected", "Sample collected", "boolean"),
            _f("not_collected_reason", "Reason not collected", "text",
               required=False),
            _f("result_available", "Result received from the laboratory", "boolean",
               derived=True,
               derivation="Set when the external laboratory transfer for this "
                          "collection event has loaded"),
            _f("any_abnormal", "Any result outside the reference range", "boolean",
               derived=True,
               derivation="Compared against the laboratory's own reference "
                          "ranges supplied with the transfer"),
            _f("clinical_significance", "Investigator assessment of any "
               "abnormality", "codelist", codelist=_CLIN_SIG, required=False,
               note="Required whenever any_abnormal = Yes. A clinically "
                    "significant abnormality must also appear as an adverse "
                    "event; this pairing is a programmed check."),
            _f("repeat_requested", "Repeat test requested", "boolean"),
            _f("pregnancy_result", "Pregnancy test result", "codelist",
               codelist=("Negative", "Positive", "Not applicable"),
               conditional_on="pregnancy_risk",
               note="A positive result hard-blocks dosing and the subject is "
                    "withdrawn and followed up."),
            _f("alt_x_uln", "ALT as a multiple of the upper limit of normal",
               "decimal", units="x ULN", derived=True,
               derivation="ALT result / the laboratory's upper reference limit",
               conditional_on="hepatotoxicity"),
            _f("ast_x_uln", "AST as a multiple of the upper limit of normal",
               "decimal", units="x ULN", derived=True,
               derivation="AST result / the laboratory's upper reference limit",
               conditional_on="hepatotoxicity"),
            _f("bilirubin_x_uln", "Total bilirubin as a multiple of the upper "
               "limit of normal", "decimal", units="x ULN", derived=True,
               derivation="Total bilirubin result / the upper reference limit",
               conditional_on="hepatotoxicity"),
            _f("alp_x_uln", "Alkaline phosphatase as a multiple of the upper "
               "limit of normal", "decimal", units="x ULN", derived=True,
               derivation="ALP result / the upper reference limit",
               conditional_on="hepatotoxicity"),
        ),
    ),
    CRFForm(
        key="ae_adverse_events",
        title="Adverse events",
        sdtm_domain="AE",
        purpose=("Records every adverse event from informed consent to the "
                 "follow-up visit, with its timing, severity, causality, "
                 "seriousness and outcome."),
        completed_at="Continuously; one record per event",
        repeating="per_record",
        critical=True,
        rationale=("The adverse event table is the primary safety output of the "
                   "study and the input to the safety database reconciliation. "
                   "In a crossover the event must be attributable to the right "
                   "PERIOD, and therefore to the right treatment, or the safety "
                   "comparison is meaningless."),
        fields=(
            _f("ae_number", "Adverse event number", "text",
               note="Sequential within subject; used as the link from "
                    "concomitant medications and from the SAE report."),
            _f("ae_term", "Adverse event (verbatim as reported)", "text",
               sdtm_variable="AE.AETERM",
               analysis_columns=("ae.ae_term",),
               note="Verbatim. Diagnoses are preferred to isolated symptoms "
                    "where a diagnosis is available."),
            _f("ae_meddra_llt", "MedDRA Lowest Level Term", "text", derived=True,
               derivation="Assigned by central medical coding",
               sdtm_variable="AE.AELLT"),
            _f("ae_meddra_pt", "MedDRA Preferred Term", "text", derived=True,
               derivation="Assigned by central medical coding",
               sdtm_variable="AE.AEDECOD"),
            _f("ae_meddra_soc", "MedDRA System Organ Class", "text", derived=True,
               derivation="Assigned by central medical coding",
               sdtm_variable="AE.AEBODSYS"),
            _f("onset_date", "Onset date", "date", sdtm_variable="AE.AESTDTC"),
            _f("onset_time", "Onset time", "time", required=False,
               note="Required for an event starting on a dosing day: in a "
                    "crossover the temporal relationship to that period's dose "
                    "is what assigns the event to a treatment."),
            _f("start_day", "Study day of onset", "integer", derived=True,
               derivation=("onset_date - first dose date + 1 when onset is on or "
                           "after first dose; onset_date - first dose date when "
                           "it is before. There is no Day 0 (SDTM --DY)."),
               analysis_columns=("ae.start_day",)),
            _f("period", "Period during which the event started", "integer",
               range_low=1, required=False,
               note="Blank only for an event that started before the first dose."),
            _f("treatment_at_onset", "Treatment in that period", "codelist",
               codelist=("T (Test)", "R (Reference)", "Not applicable (pre-dose)"),
               derived=True,
               derivation="Randomisation schedule for the subject's sequence and "
                          "the period of onset"),
            _f("treatment_emergent", "Treatment-emergent", "boolean", derived=True,
               derivation="Onset on or after the first dose, or a pre-existing "
                          "condition that worsened after it"),
            _f("resolution_date", "Resolution date", "date", required=False,
               sdtm_variable="AE.AEENDTC"),
            _f("ongoing", "Ongoing at the end of the study", "boolean",
               note="An event ongoing at the last visit must be followed until "
                    "resolution or stabilisation."),
            _f("severity", "Severity", "codelist", codelist=_SEVERITY,
               sdtm_variable="AE.AESEV",
               analysis_columns=("ae.severity",),
               note="Severity (intensity) is NOT seriousness. A severe headache "
                    "is not a serious adverse event; a brief hospitalisation for "
                    "a mild event is."),
            _f("serious", "Serious adverse event", "boolean",
               sdtm_variable="AE.AESER",
               analysis_columns=("ae.serious",)),
            _f("seriousness_criteria", "Seriousness criteria met", "codelist",
               codelist=_SERIOUSNESS, required=False,
               note="Required when serious = Yes; more than one may apply."),
            _f("causality", "Relationship to study drug", "codelist",
               codelist=_CAUSALITY, sdtm_variable="AE.AEREL",
               note=("Investigator's assessment. Every event needs one - "
                     "'unknown' is not an option, because an unassessed event "
                     "counts as related in the safety summary.")),
            _f("related", "Related (derived)", "boolean", derived=True,
               derivation=("causality in {" + ", ".join(_RELATED_TRUE_VALUES) + "}"),
               analysis_columns=("ae.related",),
               note="The collapse from the five-point scale to the binary the "
                    "analysis uses is pre-specified here, not decided later."),
            _f("action_taken", "Action taken with study drug", "codelist",
               codelist=_AE_ACTION, sdtm_variable="AE.AEACN"),
            _f("treatment_given", "Treatment given for the event", "boolean"),
            _f("outcome", "Outcome", "codelist", codelist=_AE_OUTCOME,
               sdtm_variable="AE.AEOUT",
               analysis_columns=("ae.outcome",)),
            _f("led_to_discontinuation", "Led to study discontinuation", "boolean"),
            _f("sae_reported", "Reported to the sponsor safety function", "boolean",
               required=False,
               note="Required when serious = Yes. The expedited reporting "
                    "timelines themselves are specified in the safety reporting "
                    "section of the protocol; this field exists so the clinical "
                    "database and the safety database can be reconciled."),
            _f("sae_reference_number", "Safety database reference number", "text",
               required=False,
               note="The reconciliation key between this form and the "
                    "pharmacovigilance database."),
            _f("injection_site_event", "Event is an injection-site reaction",
               "boolean", conditional_on="injection_site"),
        ),
    ),
    CRFForm(
        key="injection_site_assessment",
        title="Injection site assessment",
        sdtm_domain="CE",
        purpose=("Records the graded local tolerability assessment at each "
                 "injection site."),
        completed_at="Pre-dose and at the protocol-specified post-dose times",
        repeating="per_visit",
        conditional_on="injection_site",
        rationale=("Local reactions are the dose-limiting tolerability finding "
                   "for subcutaneous products and are graded on a scale, not "
                   "described in prose, so that they tabulate."),
        fields=(
            _f("assessment_datetime", "Date and time of assessment", "datetime"),
            _f("period", "Period number", "integer", range_low=1, required=False),
            _f("site", "Injection site assessed", "codelist",
               codelist=("Abdomen - left", "Abdomen - right", "Thigh - left",
                         "Thigh - right", "Upper arm - left", "Upper arm - right")),
            _f("erythema_mm", "Erythema, greatest diameter", "integer", units="mm",
               range_low=0, range_high=300),
            _f("induration_mm", "Induration/swelling, greatest diameter",
               "integer", units="mm", range_low=0, range_high=300),
            _f("pain_score", "Pain at the site (0-10 numeric rating scale)",
               "integer", range_low=0, range_high=10),
            _f("pruritus", "Pruritus present", "boolean"),
            _f("grade", "Toxicity grade", "codelist",
               codelist=("Grade 0 (none)", "Grade 1 (mild)", "Grade 2 (moderate)",
                         "Grade 3 (severe)", "Grade 4 (potentially life-threatening)"),
               derived=True,
               derivation=("From the measured diameter per the FDA Toxicity "
                           "Grading Scale for Healthy Adult Volunteers Enrolled "
                           "in Preventive Vaccine Clinical Trials (Sept 2007), "
                           "the conventional scale for local reactions in "
                           "healthy volunteers")),
            _f("reported_as_ae", "Reported as an adverse event", "boolean",
               note="Any grade >= 2, or any reaction the investigator judges "
                    "clinically significant, must also exist as an AE record."),
        ),
    ),
    CRFForm(
        key="liver_event_followup",
        title="Liver event follow-up (Hy's Law assessment)",
        sdtm_domain="LB",
        purpose=("Structures the repeat testing and differential-diagnosis "
                 "workup triggered by a potential drug-induced liver injury."),
        completed_at="Whenever the liver-event trigger criteria are met",
        repeating="per_record",
        conditional_on="hepatotoxicity",
        rationale=("A transaminase elevation with a rising bilirubin and no "
                   "cholestasis is the only reliable clinical predictor of "
                   "severe drug-induced liver injury, and it must be pursued "
                   "within days, not at the next scheduled visit."),
        fields=(
            _f("trigger_datetime", "Date and time the trigger criteria were met",
               "datetime"),
            _f("trigger_criterion", "Criterion met", "codelist",
               codelist=(f"ALT or AST >= {_num(HYS_LAW_TRANSAMINASE_X_ULN)} x ULN",
                         f"Total bilirubin >= {_num(HYS_LAW_BILIRUBIN_X_ULN)} x ULN",
                         "Both", "INR elevation", "Symptomatic hepatitis")),
            _f("repeat_test_datetime", "Date and time of the repeat test",
               "datetime",
               note="Repeat testing within 48-72 h of the trigger."),
            _f("alt_x_uln", "Repeat ALT", "decimal", units="x ULN"),
            _f("ast_x_uln", "Repeat AST", "decimal", units="x ULN"),
            _f("bilirubin_x_uln", "Repeat total bilirubin", "decimal", units="x ULN"),
            _f("alp_x_uln", "Repeat alkaline phosphatase", "decimal", units="x ULN"),
            _f("hys_law_case", "Meets Hy's Law laboratory criteria", "boolean",
               derived=True,
               derivation=(f"(ALT or AST) >= {_num(HYS_LAW_TRANSAMINASE_X_ULN)} x ULN "
                           f"AND total bilirubin >= {_num(HYS_LAW_BILIRUBIN_X_ULN)} "
                           f"x ULN AND ALP < {_num(HYS_LAW_ALP_X_ULN)} x ULN "
                           f"(i.e. no cholestasis)")),
            _f("alternative_etiology", "Alternative aetiology identified", "text",
               required=False,
               note="Viral hepatitis serology, alcohol history, other "
                    "hepatotoxic medications and biliary imaging are the "
                    "standard workup before a case is attributed to the drug."),
            _f("study_drug_stopped", "Study drug permanently discontinued",
               "boolean"),
        ),
    ),
    CRFForm(
        key="pd_assessment",
        title="Pharmacodynamic assessment",
        sdtm_domain="PD",
        purpose=("Records the validated pharmacodynamic measure serving as a "
                 "co-primary or supportive endpoint."),
        completed_at="At the protocol-specified pharmacodynamic timepoints",
        repeating="per_sample",
        conditional_on="pd_coprimary",
        rationale=("Where a pharmacodynamic endpoint is co-primary, its "
                   "collection has the same integrity requirements as the PK "
                   "profile: an actual clock time, a defined measurement "
                   "condition, and a pre-specified handling of missing points."),
        fields=(
            _f("period", "Period number", "integer", range_low=1),
            _f("nominal_time_h", "Nominal timepoint", "decimal", units="h"),
            _f("actual_datetime", "Actual date and time of measurement", "datetime"),
            _f("elapsed_time_h", "Elapsed time since dosing", "decimal", units="h",
               derived=True,
               derivation="actual_datetime - ex_dosing dose datetime, in hours"),
            _f("pd_test", "Pharmacodynamic measure", "text"),
            _f("pd_result", "Result", "decimal"),
            _f("pd_unit", "Unit", "text"),
            _f("measurement_valid", "Measurement valid", "boolean"),
            _f("invalid_reason", "Reason the measurement is not valid", "text",
               required=False),
        ),
    ),
    # -- Deviations and disposition ----------------------------------------
    CRFForm(
        key="dv_protocol_deviations",
        title="Protocol deviations",
        sdtm_domain="DV",
        purpose=("Records every departure from the protocol, its category and "
                 "its assessed impact on subject safety and on data integrity."),
        completed_at="As they occur; one record per deviation",
        repeating="per_record",
        rationale=("Deviations are classified as major or minor BEFORE unblinded "
                   "data are seen; that classification decides the per-protocol "
                   "population, so doing it afterwards would be a bias."),
        fields=(
            _f("deviation_date", "Date of the deviation", "date"),
            _f("period", "Period number", "integer", range_low=1, required=False),
            _f("category", "Category", "codelist",
               codelist=("Eligibility criterion not met",
                         "Informed consent process",
                         "Study drug administration (dose, timing, product)",
                         "PK sample outside the permitted window",
                         "PK sample not collected",
                         "Assessment not performed or out of window",
                         "Prohibited concomitant medication",
                         "Restriction not followed (fasting, meals, posture)",
                         "Safety reporting timeline",
                         "Other (specify)")),
            _f("description", "Description", "text"),
            _f("impact_safety", "Impact on subject safety", "boolean"),
            _f("impact_data", "Impact on data integrity or evaluability", "boolean"),
            _f("classification", "Classification", "codelist",
               codelist=("Major (important)", "Minor"),
               note="Assigned at the blinded data review, before the treatment "
                    "codes are released."),
            _f("reported_to_irb", "Reported to the IRB/IEC", "boolean"),
            _f("corrective_action", "Corrective and preventive action", "text",
               required=False),
        ),
    ),
    CRFForm(
        key="ds_period_disposition",
        title="Period disposition",
        sdtm_domain="DS",
        purpose="Records whether the subject completed each treatment period.",
        completed_at="At the end of each period",
        repeating="per_period",
        conditional_on="multi_period",
        rationale=("A subject who completes Period 1 but not Period 2 "
                   "contributes to the safety population and, in a crossover, "
                   "may contribute nothing to the bioequivalence comparison. "
                   "That distinction is made here, per period, not at the end."),
        fields=(
            _f("period", "Period number", "integer", range_low=1),
            _f("period_completed", "Period completed as planned", "boolean"),
            _f("period_end_date", "Date the period ended", "date"),
            _f("discontinuation_reason", "Reason the period was not completed",
               "codelist", codelist=_DISCONT_REASON, required=False),
            _f("dosed_in_period", "Study drug administered in this period",
               "boolean"),
            _f("pk_profile_complete", "PK profile complete for this period",
               "boolean", derived=True,
               derivation="All scheduled PK samples for the period were "
                          "collected within their windows"),
        ),
    ),
    CRFForm(
        key="ds_study_completion",
        title="Study completion / early withdrawal",
        sdtm_domain="DS",
        purpose=("Records the end of the subject's participation and the reason, "
                 "and confirms the follow-up obligations were met."),
        completed_at="At the follow-up visit or at withdrawal",
        repeating="once",
        rationale=("The disposition record is what the subject-accounting table "
                   "is built from and what determines the analysis populations; "
                   "an unexplained withdrawal cannot be classified afterwards."),
        fields=(
            _f("completed_study", "Subject completed the study as planned",
               "boolean", sdtm_variable="DS.DSDECOD"),
            _f("completion_date", "Date of study completion or withdrawal", "date",
               sdtm_variable="DS.DSSTDTC"),
            _f("discontinuation_reason", "Primary reason for discontinuation",
               "codelist", codelist=_DISCONT_REASON, required=False,
               note="Required when completed_study = No. 'Other' requires free "
                    "text; a blank leaves the subject unclassifiable in the "
                    "disposition table."),
            _f("discontinuation_detail", "Detail", "text", required=False),
            _f("related_ae_number", "Adverse event number leading to withdrawal",
               "text", required=False,
               note="Required when the reason is 'Adverse event'."),
            _f("dosed_at_least_once", "Received at least one dose", "boolean",
               derived=True,
               derivation="Any ex_dosing record exists for the subject",
               note="Defines membership of the safety population."),
            _f("last_period_completed", "Last period completed", "integer",
               range_low=0, required=False),
            _f("replaced", "Subject replaced", "boolean"),
            _f("replacement_subject_id", "Identifier of the replacement subject",
               "text", required=False),
            _f("follow_up_completed", "Follow-up assessments completed", "boolean"),
            _f("ongoing_ae_followed", "All ongoing adverse events followed to "
               "resolution or stabilisation", "boolean"),
        ),
    ),
    CRFForm(
        key="su_restrictions",
        title="Lifestyle restrictions and substance use",
        sdtm_domain="SU",
        purpose=("Confirms adherence to the dietary, xanthine, alcohol, tobacco "
                 "and activity restrictions that keep exposure comparable "
                 "between periods."),
        completed_at="Screening and each admission and dosing day",
        repeating="per_visit",
        rationale=("These restrictions exist because each of them changes drug "
                   "exposure: grapefruit inhibits intestinal CYP3A4, tobacco "
                   "induces CYP1A2, and unstandardised meals change absorption. "
                   "An unrecorded breach becomes unexplained variability."),
        fields=(
            _f("tobacco_use", "Tobacco/nicotine use", "codelist",
               codelist=("Never", "Former (stopped >= 3 months ago)", "Current"),
               sdtm_variable="SU.SUTRT"),
            _f("alcohol_abstinence_confirmed", "Alcohol abstinence confirmed for "
               "the protocol-defined period", "boolean"),
            _f("xanthine_abstinence_confirmed", "Caffeine/xanthine abstinence "
               "confirmed", "boolean"),
            _f("grapefruit_abstinence_confirmed", "Grapefruit and Seville orange "
               "abstinence confirmed", "boolean"),
            _f("strenuous_exercise_avoided", "Strenuous exercise avoided", "boolean"),
            _f("breach_description", "Description of any breach", "text",
               required=False),
        ),
    ),
)

_FORM_BY_KEY = {f.key: f for f in _CATALOGUE}


# ---------------------------------------------------------------------------
# Applicability detection - deterministic cues from the real label text
# ---------------------------------------------------------------------------
# Matched case-insensitively against the DailyMed label sections already fetched
# for the protocol. Same approach as services/stratification.py and
# services/sampling_schedule.py: a conditional form is switched on because the
# APPROVED LABEL says the hazard exists, not because a model remembered it.

_CUES: dict[str, tuple[str, ...]] = {
    "qt_prolongation": (
        r"qt(?:c)?[\s-]*(?:interval\s*)?prolong", r"torsade", r"\bqtc?\b\s*interval",
        r"proarrhythmi", r"ventricular arrhythmi",
    ),
    "hepatotoxicity": (
        r"hepatotox", r"drug[- ]induced liver injury", r"\bdili\b",
        r"hepatic failure", r"transaminase", r"aminotransferase",
        r"\balt\b[^.]{0,40}elevat", r"jaundice",
    ),
    "nephrotoxicity": (
        r"nephrotox", r"acute kidney injury", r"renal failure",
        r"serum creatinine[^.]{0,30}increase",
    ),
    "pregnancy_risk": (
        r"pregnan", r"teratogen", r"embryo[\s-]?fetal", r"contracept",
        r"fetal harm", r"\blactation\b",
    ),
    "injection_site": (
        r"injection[- ]site", r"subcutaneous", r"intramuscular",
        r"pre-?filled syringe", r"auto-?injector",
    ),
    "immunogenicity": (
        r"immunogenic", r"anti[- ]drug antibod", r"neutrali[sz]ing antibod",
    ),
    "hypersensitivity": (
        r"anaphyla", r"hypersensitivity reaction", r"angioedema",
    ),
    "controlled_substance": (
        r"schedule\s+(?:ii|iii|iv|v)\b", r"controlled substance",
        r"abuse (?:potential|liability)", r"physical dependence",
    ),
    "hypoglycaemia": (
        r"hypoglyc", r"\binsulin\b", r"blood glucose",
    ),
}


def _label_text(label: Optional[dict[str, str]]) -> str:
    """Concatenate the label sections that carry the hazard language."""
    if not label:
        return ""
    keys = (
        "boxed_warning", "warnings_and_precautions", "warnings", "adverse_reactions",
        "clinical_pharmacology", "pharmacokinetics", "dosage_and_administration",
        "use_in_specific_populations", "drug_abuse_and_dependence",
        "contraindications", "indications_usage", "how_supplied", "title",
    )
    return " ".join(str(label.get(k, "") or "") for k in keys)


def _detect(text: str) -> set[str]:
    """Which conditional requirements the label text gives positive evidence for."""
    if not text:
        return set()
    lowered = text.lower()
    hits: set[str] = set()
    for key, patterns in _CUES.items():
        if any(re.search(p, lowered) for p in patterns):
            hits.add(key)
    return hits


# Human-readable justification for every condition key, so a switched-on form
# can say WHY it is there rather than appearing without explanation.
_CONDITION_RATIONALE: dict[str, str] = {
    "qt_prolongation": (
        "The label describes QT prolongation or a proarrhythmic risk, so ECGs "
        "are collected in triplicate with a pre-specified Fridericia correction "
        "and an ICH E14 alert threshold."
    ),
    "hepatotoxicity": (
        "The label describes hepatotoxicity or transaminase elevation, so liver "
        "chemistries are expressed as multiples of the upper limit of normal and "
        "a Hy's Law follow-up form is included."
    ),
    "nephrotoxicity": (
        "The label describes renal toxicity, so creatinine is monitored with a "
        "programmed change-from-baseline check."
    ),
    "pregnancy_risk": (
        "The label describes fetal risk or requires contraception, so pregnancy "
        "testing is captured with a result that hard-blocks dosing."
    ),
    "injection_site": (
        "The product is administered by injection, so the injection site is "
        "standardised and recorded and local tolerability is graded."
    ),
    "immunogenicity": (
        "The label describes immunogenicity, so anti-drug antibody sampling is "
        "logged against a confirmed pre-dose baseline."
    ),
    "hypersensitivity": (
        "The label describes hypersensitivity or anaphylaxis, so the observation "
        "period and emergency treatment are recorded on the adverse event form."
    ),
    "controlled_substance": (
        "The label identifies a controlled substance, so drug accountability "
        "carries a witnessed, double-signed custody record."
    ),
    "hypoglycaemia": (
        "The label describes hypoglycaemia or glucose-lowering activity, so "
        "glucose monitoring is captured with a programmed alert."
    ),
    "multi_period": (
        "The design has more than one dosing period, so dosing, PK and "
        "disposition forms repeat per period and each period is dispositioned "
        "separately."
    ),
    "food_effect": (
        "This is a food-effect study, so the standardised high-fat meal and its "
        "timing relative to dosing are collected as data, not as a note."
    ),
    "multiple_dose": (
        "The study uses multiple dosing, so pre-dose trough collections are "
        "logged to demonstrate steady state."
    ),
    "pd_coprimary": (
        "A pharmacodynamic endpoint is co-primary, so it is collected with the "
        "same timing discipline as the PK profile."
    ),
    "biologic": (
        "The product is a biologic, so immunogenicity and local tolerability "
        "collection apply."
    ),
}


# ---------------------------------------------------------------------------
# Edit-check catalogue
# ---------------------------------------------------------------------------
# Static checks live here; the study-specific ones (period range, PK window,
# emesis window, sequence codelist) are generated in _build_edit_checks because
# their numbers come from THIS study's design.

_STATIC_CHECKS: tuple[EditCheck, ...] = (
    # -- Cross-form: the ones that protect the analysis --------------------
    EditCheck(
        key="pk_draw_after_dose",
        check_type="cross_form",
        severity="hard",
        forms=("pc_pk_sample_log", "ex_dosing"),
        fields=("actual_draw_date", "actual_draw_time", "dose_date", "dose_time"),
        rule=("For every PK sample with nominal_time_h > 0, the actual draw date "
              "and time must be LATER than the dose date and time for the same "
              "subject and period. For the pre-dose sample (nominal_time_h = 0) "
              "it must be EARLIER than or equal to the dose time."),
        rationale=("A post-dose sample timestamped before the dose is either a "
                   "transcription error or a clock that was never synchronised. "
                   "Either way the elapsed time - the value the entire "
                   "non-compartmental analysis is built on - is wrong, and it is "
                   "wrong in a direction that no later review can detect."),
        action="Reject the entry and require the site to correct the clock times "
               "against source before saving.",
    ),
    EditCheck(
        key="pk_dose_time_required_before_sampling",
        check_type="cross_form",
        severity="hard",
        forms=("pc_pk_sample_log", "ex_dosing"),
        fields=("dose_time", "nominal_time_h"),
        rule=("No PK sample record for a period may be saved until the dosing "
              "record for that subject and period carries a complete dose date "
              "and dose time."),
        rationale=("Without the dose time there is no time origin, so elapsed "
                   "time cannot be derived for any sample in the period."),
        action="Block entry with a message naming the missing dosing record.",
    ),
    EditCheck(
        key="pk_elapsed_time_monotonic",
        check_type="consistency",
        severity="query",
        forms=("pc_pk_sample_log",),
        fields=("nominal_time_h", "elapsed_time_h"),
        rule=("Within a subject and period, the derived elapsed times must "
              "increase strictly in the same order as the nominal timepoints."),
        rationale=("Out-of-order elapsed times mean two samples were swapped, "
                   "mislabelled or entered against the wrong row; a swapped pair "
                   "distorts both the peak and the terminal slope."),
        action="Auto-query the site listing the offending pair of timepoints.",
    ),
    EditCheck(
        key="pk_sample_unique",
        check_type="uniqueness",
        severity="hard",
        forms=("pc_pk_sample_log",),
        fields=("subject_id", "period", "nominal_time_h", "sample_id"),
        rule=("The combination (subject, period, nominal timepoint) must be "
              "unique, and sample_id must be unique across the entire study."),
        rationale=("The analysis upload rejects a duplicated "
                   "(subject, period, time); a duplicate barcode makes two "
                   "bioanalytical results unassignable to a subject."),
        action="Block the duplicate and report the first occurrence.",
    ),
    EditCheck(
        key="pk_not_collected_needs_reason",
        check_type="missing",
        severity="hard",
        forms=("pc_pk_sample_log",),
        fields=("sample_collected", "not_collected_reason"),
        rule=("If sample_collected = No then not_collected_reason must be "
              "populated; if it is 'Other', the free-text detail is mandatory."),
        rationale=("An unexplained missing concentration cannot be classified as "
                   "missing-at-random at analysis, and the protocol has to "
                   "pre-specify how missing points are handled."),
        action="Block save until the reason is entered.",
    ),
    EditCheck(
        key="pk_collected_needs_actual_time",
        check_type="missing",
        severity="hard",
        forms=("pc_pk_sample_log",),
        fields=("sample_collected", "actual_draw_date", "actual_draw_time"),
        rule=("If sample_collected = Yes then the actual draw date and time are "
              "mandatory and may not be defaulted from the nominal time."),
        rationale=("Auto-filling the nominal time is the exact failure this form "
                   "exists to prevent; it produces data that look complete and "
                   "are silently biased."),
        action="Block save; the field has no default value in the eCRF.",
    ),
    EditCheck(
        key="pk_sample_count_reconciliation",
        check_type="reconciliation",
        severity="review",
        forms=("pc_pk_sample_log",),
        fields=("sample_id", "sample_collected"),
        rule=("The number of samples marked collected must equal the number of "
              "concentration results received from the bioanalytical laboratory, "
              "matched one-to-one on sample_id."),
        rationale=("An unmatched sample means either a result was lost in "
                   "transfer or a sample was never shipped; both change the "
                   "number of evaluable profiles."),
        action="Produce a reconciliation listing for data management and the "
               "bioanalytical laboratory before database lock.",
    ),
    # -- Dosing ------------------------------------------------------------
    EditCheck(
        key="dose_once_per_period",
        check_type="uniqueness",
        severity="hard",
        forms=("ex_dosing",),
        fields=("subject_id", "period"),
        rule="Exactly one dosing record may exist per subject per period.",
        rationale=("Two dosing records for one period means either a duplicate "
                   "entry or an unreported second administration; the first "
                   "corrupts the time origin and the second is a safety event."),
        action="Block the duplicate and route to the medical monitor.",
    ),
    EditCheck(
        key="dose_matches_randomization",
        check_type="cross_form",
        severity="hard",
        forms=("ex_dosing", "dm_demographics"),
        fields=("treatment", "sequence", "period"),
        rule=("The treatment recorded for a subject and period must equal the "
              "treatment the randomisation schedule assigns to that subject's "
              "sequence in that period."),
        rationale=("A mis-dosed period is a major deviation that invalidates the "
                   "subject's contribution to the comparison and must be caught "
                   "on the day, not at database lock."),
        action="Block save and raise an immediate notification to the sponsor.",
    ),
    EditCheck(
        key="fast_duration_adequate",
        check_type="range",
        severity="query",
        forms=("ex_dosing",),
        fields=("fast_duration_h",),
        rule=(f"The derived fast duration must be at least "
              f"{_num(OVERNIGHT_FAST_HOURS)} h."),
        rationale=("A short fast changes gastric emptying and therefore the "
                   "absorption rate; because the fasting condition is what makes "
                   "periods comparable, a breach is a protocol deviation with a "
                   "data-integrity impact."),
        action="Auto-query and generate a protocol deviation record.",
    ),
    EditCheck(
        key="water_volume_fixed",
        check_type="range",
        severity="query",
        forms=("ex_dosing",),
        fields=("water_volume_ml",),
        rule=f"Water volume must equal {_num(DOSING_WATER_VOLUME_ML)} mL.",
        rationale=("Gastric fluid volume changes dissolution and therefore Cmax; "
                   "the volume is fixed by the guidance precisely so that it "
                   "cannot vary between periods or subjects."),
        action="Auto-query and generate a protocol deviation record.",
    ),
    EditCheck(
        key="expiry_after_dosing",
        check_type="range",
        severity="hard",
        forms=("ex_dosing",),
        fields=("expiry_date", "dose_date"),
        rule="expiry_date must be later than dose_date.",
        rationale=("Administration of expired product is a GMP and GCP failure "
                   "that invalidates the period."),
        action="Block save and notify the sponsor.",
    ),
    EditCheck(
        key="partial_dose_needs_reason",
        check_type="missing",
        severity="hard",
        forms=("ex_dosing",),
        fields=("dose_administered_completely", "partial_dose_reason"),
        rule=("If dose_administered_completely = No then partial_dose_reason is "
              "mandatory."),
        rationale=("A partial dose makes the exposure comparison for that period "
                   "uninterpretable; the reason decides whether the period is "
                   "excluded."),
        action="Block save until the reason is entered.",
    ),
    EditCheck(
        key="emesis_time_required",
        check_type="missing",
        severity="hard",
        forms=("ex_dosing",),
        fields=("emesis_occurred", "emesis_datetime"),
        rule="If emesis_occurred = Yes then emesis_datetime is mandatory.",
        rationale=("Exclusion for emesis depends on WHEN it happened relative to "
                   "Tmax. 'Yes' without a time cannot be adjudicated and forces "
                   "a conservative exclusion."),
        action="Block save until the time is entered.",
    ),
    # -- Adverse events ----------------------------------------------------
    EditCheck(
        key="ae_onset_not_before_consent",
        check_type="cross_form",
        severity="hard",
        forms=("ae_adverse_events", "dm_demographics"),
        fields=("onset_date", "consent_date"),
        rule="AE onset_date must be on or after the informed consent date.",
        rationale=("An event before consent is by definition medical history, "
                   "not an adverse event; mis-filing it inflates the AE table "
                   "and can create a spurious safety signal."),
        action="Block save and offer to move the record to medical history.",
    ),
    EditCheck(
        key="ae_resolution_after_onset",
        check_type="consistency",
        severity="hard",
        forms=("ae_adverse_events",),
        fields=("onset_date", "resolution_date"),
        rule=("resolution_date must be on or after onset_date, and must be "
              "blank when ongoing = Yes."),
        rationale=("A negative duration is an impossible event and usually "
                   "means the two dates were entered in the wrong fields. "
                   "Duration is summarised in the clinical study report and, "
                   "for an event around dosing, it is part of the evidence for "
                   "the causality assessment, so the error is not merely "
                   "cosmetic."),
        action="Block save.",
    ),
    EditCheck(
        key="ae_serious_needs_criteria_and_reference",
        check_type="missing",
        severity="hard",
        forms=("ae_adverse_events",),
        fields=("serious", "seriousness_criteria", "sae_reported",
                "sae_reference_number"),
        rule=("If serious = Yes then at least one seriousness criterion, "
              "sae_reported and a safety database reference number are "
              "mandatory."),
        rationale=("The reference number is the only key on which the clinical "
                   "database and the safety database can be reconciled; without "
                   "it a serious event can exist in one system and not the "
                   "other."),
        action="Block save and notify the safety function.",
    ),
    EditCheck(
        key="ae_severity_is_not_seriousness",
        check_type="consistency",
        severity="review",
        forms=("ae_adverse_events",),
        fields=("severity", "serious"),
        rule=("Flag for medical review any event recorded as Severe but not "
              "Serious, and any event recorded as Serious with severity Mild."),
        rationale=("Confusing intensity with regulatory seriousness is the "
                   "single most common adverse event coding error and it changes "
                   "expedited reporting obligations."),
        action="Route to the medical monitor for confirmation; do not auto-change.",
    ),
    EditCheck(
        key="ae_causality_mandatory",
        check_type="missing",
        severity="hard",
        forms=("ae_adverse_events",),
        fields=("causality",),
        rule="Every adverse event must carry an investigator causality "
             "assessment; there is no 'not assessed' option.",
        rationale=("An unassessed event is counted as related in the safety "
                   "summary, so leaving it blank silently worsens the drug's "
                   "safety profile."),
        action="Block save.",
    ),
    EditCheck(
        key="ae_ongoing_at_study_end",
        check_type="cross_form",
        severity="query",
        forms=("ae_adverse_events", "ds_study_completion"),
        fields=("ongoing", "outcome", "ongoing_ae_followed"),
        rule=("At study completion, every AE with ongoing = Yes must have an "
              "outcome of 'Recovering/Resolving' or 'Not recovered/Not resolved' "
              "and the disposition form must confirm follow-up."),
        rationale=("Ongoing events must be followed to resolution or "
                   "stabilisation; an unfollowed ongoing event is an "
                   "unclosed safety obligation."),
        action="Auto-query at the disposition visit.",
    ),
    EditCheck(
        key="ae_period_required_when_post_dose",
        check_type="consistency",
        severity="query",
        forms=("ae_adverse_events",),
        fields=("start_day", "period"),
        rule=("If the derived start_day is >= 1 (on or after first dose) then "
              "the period of onset must be populated."),
        rationale=("Without the period, the event cannot be attributed to a "
                   "treatment, so the by-treatment safety table cannot be "
                   "produced at all in a crossover."),
        action="Auto-query the site.",
    ),
    # -- Laboratory / vitals / ECG -----------------------------------------
    EditCheck(
        key="cs_abnormality_needs_ae",
        check_type="cross_form",
        severity="query",
        forms=("lb_laboratory", "vs_vital_signs", "eg_ecg", "ae_adverse_events"),
        fields=("clinical_significance", "interpretation"),
        rule=("Any assessment marked clinically significant must have a "
              "corresponding adverse event record with an onset date on or "
              "around the assessment date."),
        rationale=("A clinically significant abnormality that never becomes an "
                   "adverse event is a missing safety datum, and it is the "
                   "commonest reconciliation finding at inspection."),
        action="Auto-query the site to enter the AE or to justify why the "
               "finding is not an adverse event.",
    ),
    EditCheck(
        key="abnormal_result_needs_significance",
        check_type="missing",
        severity="query",
        forms=("lb_laboratory",),
        fields=("any_abnormal", "clinical_significance"),
        rule=("If any_abnormal = Yes then clinical_significance must be "
              "populated by the investigator."),
        rationale=("Out-of-range values are common and mostly meaningless; only "
                   "the investigator's judgement makes them interpretable."),
        action="Auto-query.",
    ),
    EditCheck(
        key="vitals_physiological_range",
        check_type="range",
        severity="query",
        forms=("vs_vital_signs",),
        fields=("systolic_bp", "diastolic_bp", "pulse", "respiratory_rate",
                "temperature"),
        rule=("Each vital sign must fall inside its specified entry range; a "
              "value outside it fires a query rather than being rejected, "
              "because a genuine extreme value is exactly what must not be "
              "silently blocked."),
        rationale=("Range checks catch transposed digits (e.g. 210/80 typed for "
                   "120/80) without preventing the recording of a real clinical "
                   "event."),
        action="Auto-query with the entered value echoed back.",
    ),
    EditCheck(
        key="bp_systolic_above_diastolic",
        check_type="consistency",
        severity="hard",
        forms=("vs_vital_signs",),
        fields=("systolic_bp", "diastolic_bp"),
        rule="systolic_bp must be greater than diastolic_bp.",
        rationale="Catches the transposed entry that a range check alone misses.",
        action="Block save.",
    ),
    EditCheck(
        key="bmi_matches_height_weight",
        check_type="consistency",
        severity="hard",
        forms=("dm_demographics",),
        fields=("height_cm", "weight_kg", "bmi"),
        rule=("BMI is derived by the database from height and weight and is not "
              "editable; entry of height or weight outside the permitted range "
              "is blocked."),
        rationale=("A hand-calculated BMI is a frequent eligibility error, and "
                   "BMI is an inclusion criterion, so the error enrols an "
                   "ineligible subject."),
        action="Derive, do not collect.",
    ),
    EditCheck(
        key="ecg_qtcf_alert",
        check_type="range",
        severity="review",
        forms=("eg_ecg",),
        fields=("qtcf", "qtcf_change_from_baseline"),
        rule=(f"Flag any QTcF > {_num(QTCF_ALERT_MS)} ms, or an increase from "
              f"the period baseline > {_num(QTCF_CHANGE_FROM_BASELINE_ALERT_MS)} "
              f"ms."),
        rationale=("These are the ICH E14 values of clinical concern; they "
                   "require an immediate clinical response, not a data query "
                   "days later."),
        action="Immediate notification to the investigator and medical monitor; "
               "the record is retained as entered.",
    ),
    EditCheck(
        key="ecg_interval_consistency",
        check_type="consistency",
        severity="query",
        forms=("eg_ecg",),
        fields=("qt_interval", "qrs_duration", "heart_rate"),
        rule="QT must be greater than QRS, and both must be consistent with the "
             "recorded heart rate.",
        rationale=("Catches interval values typed into the wrong field, which "
                   "is easy to do on a machine printout carrying four intervals "
                   "in a column. A QT entered where QRS belongs would look like "
                   "a dramatic and entirely spurious cardiac finding."),
        action="Auto-query.",
    ),
    # -- Eligibility, medications, disposition -----------------------------
    EditCheck(
        key="consent_precedes_procedures",
        check_type="cross_form",
        severity="hard",
        forms=("dm_demographics", "lb_laboratory", "vs_vital_signs", "eg_ecg"),
        fields=("consent_date", "consent_time", "collection_datetime"),
        rule=("No study assessment may carry a date and time earlier than the "
              "informed consent date and time."),
        rationale=("Conducting a study-specific procedure before consent is a "
                   "serious GCP violation reportable to the IRB/IEC."),
        action="Block save and notify the sponsor.",
    ),
    EditCheck(
        key="ineligible_not_dosed",
        check_type="cross_form",
        severity="hard",
        forms=("ie_eligibility", "ex_dosing"),
        fields=("eligible", "dose_date"),
        rule=("No dosing record may be entered for a subject whose latest "
              "eligibility assessment has eligible = No."),
        rationale=("Dosing an ineligible subject is the archetypal major "
                   "deviation; the check has to fire before the dose, not "
                   "after."),
        action="Block save and notify the sponsor immediately.",
    ),
    EditCheck(
        key="prohibited_medication_flag",
        check_type="cross_form",
        severity="review",
        forms=("cm_concomitant_medications", "dv_protocol_deviations"),
        fields=("prohibited", "start_date", "end_date"),
        rule=("Any medication flagged prohibited whose use overlaps the "
              "protocol-defined restriction window must have a corresponding "
              "protocol deviation record."),
        rationale=("A prohibited concomitant medication - typically an inhibitor "
                   "or inducer of the clearance pathway - can move exposure by "
                   "more than the difference the study is powered to detect."),
        action="Route to data management to raise the deviation and to the "
               "blinded data review for an evaluability decision.",
    ),
    EditCheck(
        key="cm_for_ae_links_to_ae",
        check_type="cross_form",
        severity="query",
        forms=("cm_concomitant_medications", "ae_adverse_events"),
        fields=("given_for_ae", "ae_number"),
        rule=("If given_for_ae = Yes then ae_number must reference an existing "
              "adverse event for the same subject."),
        rationale=("A treatment given for an event that does not exist in the AE "
                   "table means the event was never reported."),
        action="Auto-query.",
    ),
    EditCheck(
        key="withdrawal_needs_reason_and_date",
        check_type="missing",
        severity="hard",
        forms=("ds_study_completion",),
        fields=("completed_study", "discontinuation_reason", "completion_date"),
        rule=("If completed_study = No then discontinuation_reason and "
              "completion_date are mandatory; if the reason is 'Adverse event' "
              "then related_ae_number is mandatory."),
        rationale=("An unclassified withdrawal cannot be placed in the "
                   "disposition table and hides a possible safety-driven "
                   "dropout."),
        action="Block save.",
    ),
    EditCheck(
        key="all_required_fields_at_visit_close",
        check_type="missing",
        severity="query",
        forms=("dm_demographics", "ie_eligibility", "ex_dosing",
               "pc_pk_sample_log", "vs_vital_signs", "eg_ecg", "lb_laboratory",
               "ae_adverse_events", "ds_study_completion"),
        fields=("*required*",),
        rule=("When a visit is marked complete, every field flagged required on "
              "every form belonging to that visit must be populated or carry an "
              "explicit not-done reason."),
        rationale=("Systematic missing-data queries at visit close are far "
                   "cheaper to resolve than the same queries months later, when "
                   "the source note has been filed and the memory has gone."),
        action="Auto-query per field, batched into one query per form.",
    ),
    # -- Conditional -------------------------------------------------------
    EditCheck(
        key="pregnancy_positive_blocks_dosing",
        check_type="cross_form",
        severity="hard",
        forms=("lb_laboratory", "ex_dosing"),
        fields=("pregnancy_result", "dose_date"),
        rule=("If the most recent pregnancy test before dosing is Positive, no "
              "dosing record may be saved for that subject."),
        rationale=("Dosing a pregnant subject with a product carrying fetal-risk "
                   "labelling is an unrecoverable harm, so the control belongs "
                   "in the system, not only in the procedure."),
        action="Block save unconditionally and notify the investigator.",
        conditional_on="pregnancy_risk",
    ),
    EditCheck(
        key="hys_law_trigger",
        check_type="range",
        severity="review",
        forms=("lb_laboratory", "liver_event_followup"),
        fields=("alt_x_uln", "ast_x_uln", "bilirubin_x_uln", "alp_x_uln"),
        rule=(f"Flag when (ALT or AST) >= {_num(HYS_LAW_TRANSAMINASE_X_ULN)} x "
              f"ULN, or total bilirubin >= {_num(HYS_LAW_BILIRUBIN_X_ULN)} x ULN; "
              f"classify as a potential Hy's Law case when both hold and ALP is "
              f"< {_num(HYS_LAW_ALP_X_ULN)} x ULN."),
        rationale=("Hy's Law is the strongest clinical predictor of severe "
                   "drug-induced liver injury and requires repeat testing within "
                   "48-72 hours, which only happens if the trigger is automatic."),
        action="Immediate notification to the investigator and medical monitor; "
               "open the liver event follow-up form.",
        conditional_on="hepatotoxicity",
    ),
    EditCheck(
        key="creatinine_rise_alert",
        check_type="range",
        severity="review",
        forms=("lb_laboratory",),
        fields=("any_abnormal", "clinical_significance"),
        rule=("Flag any serum creatinine increase of >= 0.3 mg/dL from the "
              "screening value, or >= 1.5 x the screening value."),
        rationale=("These are the standard acute kidney injury thresholds; for a "
                   "drug with renal toxicity in its label they must be detected "
                   "on the day the result arrives."),
        action="Notify the investigator and repeat the test.",
        conditional_on="nephrotoxicity",
    ),
    EditCheck(
        key="glucose_alert",
        check_type="range",
        severity="review",
        forms=("lb_laboratory", "pd_assessment"),
        fields=("any_abnormal", "pd_result"),
        rule=("Flag any blood glucose below 70 mg/dL (3.9 mmol/L), and treat "
              "below 54 mg/dL (3.0 mmol/L) as clinically significant "
              "hypoglycaemia requiring immediate intervention."),
        rationale=("These are the standard level-1 and level-2 hypoglycaemia "
                   "thresholds; for a glucose-lowering product they are a "
                   "real-time safety control, not a data check."),
        action="Immediate notification to site staff; record as an adverse event "
               "where the criteria are met.",
        conditional_on="hypoglycaemia",
    ),
    EditCheck(
        key="meal_timing_food_effect",
        check_type="cross_form",
        severity="query",
        forms=("ex_dosing",),
        fields=("meal_start_time", "meal_end_time", "meal_percent_consumed",
                "dose_time"),
        rule=(f"The meal must be consumed within "
              f"{FOOD_EFFECT_MEAL_DURATION_MIN} minutes, "
              f"meal_percent_consumed must be 100, and the dose must be given "
              f"{FOOD_EFFECT_DOSE_AFTER_MEAL_START_MIN} minutes after the START "
              f"of the meal."),
        rationale=("The fed condition is defined by the meal and its timing; if "
                   "either varies, the subject's fed exposure is not comparable "
                   "with anyone else's and the food-effect estimate is diluted."),
        action="Auto-query and generate a protocol deviation record.",
        conditional_on="food_effect",
    ),
    EditCheck(
        key="ada_baseline_required",
        check_type="cross_form",
        severity="query",
        forms=("is_immunogenicity_log",),
        fields=("baseline_sample", "sample_collected"),
        rule=("A subject with any post-dose immunogenicity sample must have a "
              "collected pre-dose baseline sample."),
        rationale=("Treatment-emergent anti-drug antibody status is undefined "
                   "without a baseline, so a positive post-dose result becomes "
                   "uninterpretable."),
        action="Auto-query.",
        conditional_on="immunogenicity",
    ),
    EditCheck(
        key="injection_site_grade_needs_ae",
        check_type="cross_form",
        severity="query",
        forms=("injection_site_assessment", "ae_adverse_events"),
        fields=("grade", "reported_as_ae"),
        rule=("Any injection-site assessment of Grade 2 or higher must have a "
              "corresponding adverse event record."),
        rationale=("Local tolerability is a primary safety consideration for an "
                   "injected product; a graded reaction that never reaches the "
                   "AE table understates it."),
        action="Auto-query.",
        conditional_on="injection_site",
    ),
    EditCheck(
        key="controlled_substance_accountability_balanced",
        check_type="consistency",
        severity="hard",
        forms=("da_drug_accountability",),
        fields=("quantity_received", "quantity_dispensed", "quantity_returned",
                "quantity_destroyed", "safe_log_signed"),
        rule=("Received must equal dispensed plus returned plus destroyed, and "
              "every transaction must carry two authorised signatures."),
        rationale=("Controlled-substance custody is a legal obligation "
                   "independent of GCP and is reconciled unit by unit."),
        action="Block save on an unbalanced reconciliation.",
        conditional_on="controlled_substance",
    ),
)


# ---------------------------------------------------------------------------
# Downstream analysis columns not supplied by a CRF field
# ---------------------------------------------------------------------------
# Concentrations and derived PK parameters are the important honesty point: the
# CRF does NOT collect them, so claiming coverage from a form would be false.
# They are declared here with their real origin, and the DMP specifies the
# transfer/derivation that produces them.

_NON_CRF_ORIGINS: tuple[AnalysisColumnOrigin, ...] = (
    AnalysisColumnOrigin(
        column="nca.concentration",
        source="external",
        origin="Bioanalytical laboratory electronic data transfer",
        note=("Produced by the validated bioanalytical method, transferred under "
              "a Data Transfer Agreement and joined to the PK sample log on "
              "sample_id. Below-limit-of-quantification results are transferred "
              "as the token BLQ, never as zero, so they cannot bias the AUC."),
    ),
    AnalysisColumnOrigin(
        column="params.cmax",
        source="derived",
        origin="Non-compartmental analysis of the concentration-time profile",
        note="Highest measurable concentration; not collected on any form.",
    ),
    AnalysisColumnOrigin(
        column="params.auc0t",
        source="derived",
        origin="Non-compartmental analysis (linear-up/log-down trapezoidal)",
        note="Integrated against the ACTUAL elapsed times from the PK sample log.",
    ),
    AnalysisColumnOrigin(
        column="params.auc0inf",
        source="derived",
        origin="Non-compartmental analysis (AUC0-t + Clast/lambda_z)",
        note="Requires an estimable terminal slope, hence the terminal-point "
             "requirement in the sampling schedule.",
    ),
    AnalysisColumnOrigin(
        column="params.tmax",
        source="derived",
        origin="Non-compartmental analysis",
        note="The ACTUAL elapsed time of the maximum concentration, which is why "
             "a nominal-time-only log makes Tmax wrong.",
    ),
    AnalysisColumnOrigin(
        column="params.thalf",
        source="derived",
        origin="Non-compartmental analysis (ln2 / lambda_z)",
    ),
)

_NON_CRF_BY_COLUMN = {o.column: o for o in _NON_CRF_ORIGINS}


# ---------------------------------------------------------------------------
# Context extraction
# ---------------------------------------------------------------------------


def _dig(ctx: dict[str, Any], *names: str) -> Any:
    """Look a key up in the generation context, then in population_config.

    The generation context nests the user-supplied hints under
    ``population_config`` and the computed engines under underscore-prefixed
    keys, so a caller may reasonably pass either shape.
    """
    if not ctx:
        return None
    pop = ctx.get("population_config") or {}
    for n in names:
        if ctx.get(n) is not None:
            return ctx[n]
        if isinstance(pop, dict) and pop.get(n) is not None:
            return pop[n]
    return None


def _decision_value(ctx: dict[str, Any], key: str) -> str:
    """Value of one of the five locked clinical decisions, when present."""
    decisions = _dig(ctx, "design_decisions", "_design_decisions") or []
    if isinstance(decisions, list):
        for d in decisions:
            if isinstance(d, dict) and d.get("key") == key:
                return str(d.get("value") or "")
    return ""


# The pd_endpoint decision carries a real marker ("Glucose infusion rate
# (euglycemic clamp)") or the explicit sentinel "None (PK only)" produced by
# services.design_decisions when no validated PD marker responds inside the PK
# window. Treating that sentinel as a marker would put a pharmacodynamic
# collection form into a PK-only protocol, so it is matched explicitly rather
# than by a substring guess.
_NO_PD_VALUES = ("", "none", "none (pk only)", "n/a", "not applicable",
                 "no pd endpoint", "pk only")


def _has_pd_coprimary(pd_value: Optional[str]) -> bool:
    """True only when a real validated PD marker was selected as co-primary."""
    v = (pd_value or "").strip().lower()
    if v in _NO_PD_VALUES:
        return False
    return not v.startswith("none")


def _periods_for(design: str, sampling: Optional[dict[str, Any]]) -> int:
    """Number of dosing periods each subject completes.

    Taken from the sampling schedule when it is available so the two engines
    cannot disagree; otherwise from the design key, using the same mapping.
    """
    if sampling and sampling.get("periods"):
        try:
            return max(1, int(sampling["periods"]))
        except (TypeError, ValueError):
            pass
    d = (design or "").strip().lower()
    if d == "parallel":
        return 1
    if d == "replicate":
        return 4          # the full 4-period, 2-sequence replicate design
    return 2              # standard 2x2x2 crossover


def _sequences_for(design: str, periods: int) -> tuple[str, ...]:
    """Sequence codelist for the demographics form, from the design."""
    d = (design or "").strip().lower()
    if d == "parallel":
        return ("T", "R")
    if d == "replicate" and periods >= 4:
        return ("TRTR", "RTRT")
    if d == "replicate":
        return ("TRR", "RTR", "RRT")
    return ("TR", "RT")


# ---------------------------------------------------------------------------
# Visit schedule
# ---------------------------------------------------------------------------


def _washout_days(t_half_hours: Optional[float], design: str) -> Optional[float]:
    """Washout between periods, on the SAME arithmetic the protocol locks.

    ``ai_service._locked_design_directive`` states washout as 7 x t1/2 and
    instructs the model to use that exact figure everywhere. Re-deriving it
    differently here would put the schedule of assessments in conflict with the
    study design section, so the constant is imported rather than restated.
    """
    if (design or "").strip().lower() == "parallel":
        return None
    if not t_half_hours or float(t_half_hours) <= 0:
        return None
    return round(WASHOUT_HALF_LIVES * float(t_half_hours) / 24.0, 1)


def _visit_name(prefix: str, rest: str) -> str:
    """Compose a visit name, capitalising only when there is no period prefix.

    ``str.capitalize()`` would lower-case the rest of the string and turn
    "Day -1" into "day -1", so the first character is raised by hand.
    """
    if prefix:
        return f"{prefix}{rest}"
    return rest[:1].upper() + rest[1:]


def _visit_forms(base: list[str], active: set[str]) -> list[str]:
    """Filter a visit's form list to the forms that are actually switched on."""
    return [k for k in base
            if k in _FORM_BY_KEY
            and (not _FORM_BY_KEY[k].conditional_on
                 or _FORM_BY_KEY[k].conditional_on in active)]


def _build_visit_schedule(
    *,
    periods: int,
    design: str,
    sampling: Optional[dict[str, Any]],
    washout_days: Optional[float],
    active: set[str],
) -> list[dict[str, Any]]:
    """Build the visit structure from THIS study's periods and sampling horizon.

    In-house confinement runs from admission the evening before dosing through
    the CONFINEMENT_DISCHARGE_HOURS draw; any later timepoint becomes an
    ambulatory visit on its own study day. That split is what makes the schedule
    of assessments match the sampling schedule instead of paraphrasing it.
    """
    visits: list[dict[str, Any]] = []
    visits.append({
        "key": "screening",
        "name": "Screening",
        "timing": f"Day -{SCREENING_WINDOW_DAYS} to Day -2",
        "epoch": "SCREENING",
        "forms": _visit_forms([
            "dm_demographics", "ie_eligibility", "mh_medical_history",
            "cm_concomitant_medications", "vs_vital_signs", "eg_ecg",
            "lb_laboratory", "su_restrictions", "ae_adverse_events",
        ], active),
        "note": ("Informed consent is obtained before any study-specific "
                 "procedure."),
    })

    timepoints = list((sampling or {}).get("timepoints") or [])
    last_hours = float((sampling or {}).get("last_timepoint_hours") or 0.0)
    if timepoints and not last_hours:
        last_hours = max(float(t.get("hours", 0.0)) for t in timepoints)
    discharge_h = (min(last_hours, CONFINEMENT_DISCHARGE_HOURS)
                   if last_hours else CONFINEMENT_DISCHARGE_HOURS)
    in_house = [t for t in timepoints
                if float(t.get("hours", 0.0)) <= discharge_h + 1e-9]
    ambulatory = [t for t in timepoints
                  if float(t.get("hours", 0.0)) > discharge_h + 1e-9]

    for p in range(1, periods + 1):
        label = f"Period {p} " if periods > 1 else ""
        visits.append({
            "key": f"p{p}_admission",
            "name": _visit_name(label, "admission (Day -1)"),
            "timing": (f"Day -1 of period {p}; admission at least "
                       f"{_num(CONFINEMENT_ADMISSION_HOURS_PRE_DOSE)} h before "
                       f"dosing"),
            "epoch": f"TREATMENT PERIOD {p}",
            "forms": _visit_forms([
                "ie_eligibility", "cm_concomitant_medications", "vs_vital_signs",
                "lb_laboratory", "su_restrictions", "ae_adverse_events",
                "da_drug_accountability",
            ], active),
            "note": ("Eligibility re-confirmed, urine drug and alcohol screen, "
                     "pregnancy test where applicable, start of the "
                     f"{_num(OVERNIGHT_FAST_HOURS)} h overnight fast."),
        })
        visits.append({
            "key": f"p{p}_dosing",
            "name": _visit_name(label, "Day 1 - dosing and PK sampling"),
            "timing": (f"Day 1 of period {p}; dosing, then "
                       f"{len(in_house) if in_house else 'the scheduled'} "
                       f"in-house PK draws"),
            "epoch": f"TREATMENT PERIOD {p}",
            "forms": _visit_forms([
                "ex_dosing", "pc_pk_sample_log", "vs_vital_signs", "eg_ecg",
                "ae_adverse_events", "cm_concomitant_medications",
                "is_immunogenicity_log", "injection_site_assessment",
                "pd_assessment", "dv_protocol_deviations",
            ], active),
            "note": ("The dose clock time recorded here is the time origin for "
                     "every PK sample in this period."),
        })
        if last_hours > 24.0:
            visits.append({
                "key": f"p{p}_confinement",
                "name": _visit_name(label, "in-house PK sampling"),
                "timing": (f"Through the {_num(discharge_h)} h draw, then "
                           f"discharge"),
                "epoch": f"TREATMENT PERIOD {p}",
                "forms": _visit_forms([
                    "pc_pk_sample_log", "vs_vital_signs", "ae_adverse_events",
                    "cm_concomitant_medications",
                ], active),
                "note": "Discharge only after the scheduled draw is complete.",
            })
        # Ambulatory return visits, grouped by study day.
        days: list[int] = []
        for t in ambulatory:
            d = int(t.get("day") or math.ceil(float(t.get("hours", 0.0)) / 24.0))
            if d not in days:
                days.append(d)
        for d in days:
            n_draws = sum(
                1 for t in ambulatory
                if int(t.get("day") or math.ceil(float(t.get("hours", 0.0)) / 24.0)) == d
            )
            visits.append({
                "key": f"p{p}_ambulatory_d{d}",
                "name": _visit_name(label, f"ambulatory PK visit, Day {d}"),
                "timing": f"Day {d} of period {p}",
                "epoch": f"TREATMENT PERIOD {p}",
                "forms": _visit_forms([
                    "pc_pk_sample_log", "ae_adverse_events",
                    "cm_concomitant_medications",
                ], active),
                "note": f"{n_draws} scheduled draw(s); the permitted window for "
                        f"each is fixed by the sampling schedule.",
            })
        if periods > 1:
            visits.append({
                "key": f"p{p}_disposition",
                "name": _visit_name(label, "period disposition"),
                "timing": f"End of period {p}",
                "epoch": f"TREATMENT PERIOD {p}",
                "forms": _visit_forms(["ds_period_disposition",
                                       "ae_adverse_events"], active),
                "note": "",
            })
        if periods > 1 and p < periods:
            visits.append({
                "key": f"washout_{p}",
                "name": f"Washout after period {p}",
                "timing": (f"{_num(washout_days)} days"
                           if washout_days else
                           "Protocol-specified washout"),
                "epoch": "WASHOUT",
                "forms": _visit_forms(["ae_adverse_events",
                                       "cm_concomitant_medications"], active),
                "note": ("Adverse events and concomitant medications continue to "
                         "be collected throughout the washout."),
            })

    visits.append({
        "key": "follow_up",
        "name": "Follow-up / end of study",
        "timing": (f"{FOLLOW_UP_DAYS_AFTER_LAST_DOSE} (+/- "
                   f"{FOLLOW_UP_WINDOW_DAYS}) days after the last dose"),
        "epoch": "FOLLOW-UP",
        "forms": _visit_forms([
            "vs_vital_signs", "eg_ecg", "lb_laboratory", "ae_adverse_events",
            "cm_concomitant_medications", "ds_study_completion",
            "is_immunogenicity_log",
        ], active),
        "note": ("Any adverse event ongoing at this visit is followed until "
                 "resolution or stabilisation."),
    })
    return visits


# ---------------------------------------------------------------------------
# Study-specific edit checks
# ---------------------------------------------------------------------------


def _build_edit_checks(
    *,
    active: set[str],
    periods: int,
    sampling: Optional[dict[str, Any]],
    sequences: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Static checks filtered to the active conditions, plus the derived ones.

    The derived checks carry THIS study's numbers - the period range, the
    per-timepoint windows and the emesis exclusion window - so a site cannot
    enter a period 3 in a two-period study and an out-of-window draw fires
    against the window that this schedule actually specifies.
    """
    checks = [c.to_dict() for c in _STATIC_CHECKS
              if not c.conditional_on or c.conditional_on in active]

    checks.append(EditCheck(
        key="period_within_design",
        check_type="range",
        severity="hard",
        forms=("ex_dosing", "pc_pk_sample_log", "ds_period_disposition"),
        fields=("period",),
        rule=f"period must be an integer between 1 and {periods}.",
        rationale=("The design fixes the number of periods; a value outside it "
                   "means the record is attached to a period that does not "
                   "exist and would be dropped at analysis."),
        action="Block save.",
    ).to_dict())

    checks.append(EditCheck(
        key="sequence_within_design",
        check_type="range",
        severity="hard",
        forms=("dm_demographics",),
        fields=("sequence",),
        rule=("sequence must be one of: " + ", ".join(sequences) +
              ", and is populated from the randomisation schedule rather than "
              "typed."),
        rationale=("The sequence determines which treatment each period "
                   "receives; a free-text sequence breaks that mapping for the "
                   "whole subject."),
        action="Restrict the field to the codelist.",
    ).to_dict())

    if sampling and sampling.get("timepoints"):
        windows = sorted({int(t.get("window_minutes") or 0)
                          for t in sampling["timepoints"]
                          if t.get("window_minutes")})
        window_text = (", ".join(f"{w} min" for w in windows)
                       if windows else "the protocol-specified window")
        checks.append(EditCheck(
            key="pk_draw_within_window",
            check_type="range",
            severity="query",
            forms=("pc_pk_sample_log",),
            fields=("deviation_minutes", "within_window", "deviation_reason"),
            rule=("|actual elapsed time - nominal time| must not exceed the "
                  "permitted window for that timepoint (this study uses "
                  f"{window_text}); outside it, deviation_reason becomes "
                  "mandatory and a protocol deviation is raised."),
            rationale=("Early timepoints carry the tightest windows because the "
                       "concentration is changing fastest there; the same "
                       "absolute delay that is trivial on the terminal tail can "
                       "move an absorption-phase concentration substantially."),
            action="Auto-query, require the reason, and raise a protocol "
                   "deviation record.",
        ).to_dict())

        n_expected = len(sampling["timepoints"])
        checks.append(EditCheck(
            key="pk_profile_complete",
            check_type="consistency",
            severity="query",
            forms=("pc_pk_sample_log",),
            fields=("nominal_time_h", "sample_collected"),
            rule=(f"Each subject and period must have exactly {n_expected} PK "
                  f"sample records, one per scheduled timepoint, each either "
                  f"collected or carrying a not-collected reason."),
            rationale=("A missing row is invisible; a row that is present and "
                       "marked not collected is a datum. The count is fixed by "
                       "the locked sampling schedule."),
            action="Auto-query at period close listing the missing timepoints.",
        ).to_dict())

    tmax = (sampling or {}).get("tmax_hours")
    if tmax:
        window_h = EMESIS_EXCLUSION_TMAX_MULTIPLE * float(tmax)
        checks.append(EditCheck(
            key="emesis_within_exclusion_window",
            check_type="cross_form",
            severity="review",
            forms=("ex_dosing", "dv_protocol_deviations"),
            fields=("emesis_occurred", "emesis_elapsed_h"),
            rule=(f"Flag any emesis occurring at or before "
                  f"{_num(round(window_h, 2))} h after dosing "
                  f"({_num(EMESIS_EXCLUSION_TMAX_MULTIPLE)} x the literature "
                  f"median Tmax of {_num(round(float(tmax), 2))} h)."),
            rationale=("For an immediate-release product, data from a subject "
                       "who vomits inside this window are excluded from the "
                       "statistical analysis; the decision is pre-specified here "
                       "so it is not made after seeing the concentrations."),
            action="Route to the blinded data review for the evaluability "
                   "decision; record a protocol deviation.",
        ).to_dict())

    return checks


# ---------------------------------------------------------------------------
# Downstream coverage
# ---------------------------------------------------------------------------


def _analysis_coverage(forms: list[dict[str, Any]]) -> dict[str, Any]:
    """Map every downstream analysis column to the thing that produces it.

    This is the bridge check. ``dataset_ingest`` defines what an uploadable
    study dataset must contain and ``nca`` consumes it; a CRF that does not
    reach every one of those columns produces a study this product cannot
    analyse. Anything unmatched is reported as uncovered rather than quietly
    omitted.
    """
    by_column: dict[str, dict[str, Any]] = {}
    for form in forms:
        for field in form["fields"]:
            for col in field.get("analysis_columns", []):
                by_column[col] = {
                    "column": col,
                    "source": "derived (CRF)" if field.get("derived") else "crf",
                    "form": form["key"],
                    "field": field["name"],
                    "note": field.get("derivation") or field.get("note", ""),
                }

    coverage: dict[str, Any] = {}
    uncovered: list[str] = []
    for kind, required in _ANALYSIS_REQUIRED_COLUMNS.items():
        optional = _ANALYSIS_OPTIONAL_COLUMNS.get(kind, [])
        entries: list[dict[str, Any]] = []
        for col in list(required) + list(optional):
            qualified = f"{kind}.{col}"
            hit = by_column.get(qualified)
            if hit is None and qualified in _NON_CRF_BY_COLUMN:
                o = _NON_CRF_BY_COLUMN[qualified]
                hit = {
                    "column": qualified,
                    "source": o.source,
                    "form": "",
                    "field": "",
                    "note": f"{o.origin}. {o.note}".strip(),
                }
            entry = {
                "column": col,
                "required": col in required,
                "covered": hit is not None,
            }
            if hit:
                entry.update({k: v for k, v in hit.items() if k != "column"})
            else:
                if col in required:
                    uncovered.append(qualified)
            entries.append(entry)
        coverage[kind] = entries

    return {
        "by_kind": coverage,
        "uncovered_required_columns": uncovered,
        "is_superset": not uncovered,
        "statement": (
            "Every column required by the study-results upload contract "
            "(services/dataset_ingest) and by the non-compartmental analysis "
            "(services/nca) is produced by a named CRF field, a named "
            "derivation, or a named external data transfer."
            if not uncovered else
            "The specification does NOT yet reach every required analysis "
            "column: " + ", ".join(uncovered)
        ),
    }


# ---------------------------------------------------------------------------
# Data Management Plan
# ---------------------------------------------------------------------------


_CODING_DICTIONARIES: tuple[dict[str, Any], ...] = (
    {
        "scope": "Adverse events and medical history",
        "dictionary": "MedDRA (Medical Dictionary for Regulatory Activities)",
        "release_cadence": MEDDRA_RELEASE_CADENCE,
        "version_policy": (
            "A single version is fixed at database build and used for the whole "
            "study. If a mid-study upversion is unavoidable, the entire dataset "
            "is re-coded to the new version and the change is documented and "
            "approved by the sponsor - partial re-coding would make the adverse "
            "event frequency table internally inconsistent."
        ),
        "levels_reported": "Lowest Level Term, Preferred Term, System Organ Class",
        "uncoded_handling": (
            "Terms that do not auto-code are coded manually by a qualified "
            "coder; ambiguous verbatim terms are queried to the site rather "
            "than interpreted, because a coder guessing at a verbatim term is "
            "creating clinical data."
        ),
    },
    {
        "scope": "Prior and concomitant medications",
        "dictionary": "WHODrug Global",
        "release_cadence": WHODRUG_RELEASE_CADENCE,
        "version_policy": (
            "A single version is fixed at database build; the format (B3/C3 or "
            "Global) and the version are recorded in the define.xml and in the "
            "clinical study report."
        ),
        "levels_reported": "Drug Record Number, preferred name, ATC classification",
        "uncoded_handling": (
            "Trade names and multi-ingredient products are coded to the "
            "corresponding WHODrug entry; herbal and over-the-counter products "
            "are coded, not discarded, because they carry the interaction risk "
            "the concomitant medication form exists to detect."
        ),
    },
)


def _dmp_sections(
    *,
    design: str,
    periods: int,
    n_enrolled: Optional[int],
    active: set[str],
    lloq: Optional[float],
) -> tuple[DMPSection, ...]:
    """The DMP sections, with the study-specific numbers already substituted."""
    external_sources = [
        "Bioanalytical laboratory: concentration results, joined on sample_id",
        "Central clinical laboratory: haematology, chemistry, urinalysis and "
        "serology results with their reference ranges",
        "Randomisation schedule: subject sequence and per-period treatment",
    ]
    if "immunogenicity" in active:
        external_sources.append(
            "Immunogenicity laboratory: tiered ADA screening, confirmatory and "
            "neutralising antibody results")
    if "qt_prolongation" in active:
        external_sources.append(
            "Central ECG reader: interval measurements and interpretation")

    lloq_text = (
        f"The lower limit of quantification is {lloq}; values below it are "
        f"transferred as the token BLQ."
        if lloq else
        "The lower limit of quantification is fixed by the validated "
        "bioanalytical method and must be stated in the Data Transfer "
        "Agreement before the first transfer."
    )

    return (
        DMPSection(
            key="scope_and_roles",
            title="Scope, roles and responsibilities",
            content=(
                "This plan governs the collection, validation, coding, "
                "reconciliation, locking and archival of all clinical data for "
                f"this {design} study of {periods} treatment period(s)"
                + (f" in {n_enrolled} enrolled subjects" if n_enrolled else "")
                + ". Named roles: the sponsor (accountable for the data and for "
                "the lock decision), the data manager (owner of this plan, the "
                "database and the query process), the clinical database "
                "programmer (build and validation of the edit checks), the "
                "medical coder, the medical monitor (clinical review and the "
                "adjudication of medically significant findings), the "
                "biostatistician (analysis populations and the specification of "
                "derived variables), the pharmacovigilance function (safety "
                "database and reconciliation), and the investigator (accuracy, "
                "completeness and timeliness of the site's data). Each role is "
                "held by a named individual on a signed delegation log."
            ),
            deliverables=("Signed Data Management Plan", "Delegation of "
                          "authority log", "Study-specific training records"),
            responsible="Data manager, with sponsor approval",
            timing="Approved before first subject enrolled; version-controlled "
                   "thereafter",
            reference="ICH E6(R2) 5.5, 5.1",
        ),
        DMPSection(
            key="data_flow",
            title="Data flow",
            content=(
                "Data originate as source records at the site (medical records, "
                "clinical notes, the dosing worksheet, the timed blood-draw "
                "worksheet, laboratory reports and instrument printouts). Site "
                "staff transcribe them into the validated electronic CRF, which "
                "applies the programmed edit checks at the point of entry. "
                "Externally generated data - the bioanalytical concentrations "
                "above all - never pass through the CRF: they are transferred "
                "electronically from the originating laboratory under a Data "
                "Transfer Agreement and are loaded into the clinical database "
                "against the sample and subject identifiers. Where a value is "
                "captured directly in the eCRF with no prior written or "
                "electronic record, that eCRF field is designated source data "
                "in advance and listed in the source data agreement, because "
                "an undeclared direct-entry field is a monitoring finding. "
                "The direction of flow is one-way: no analysis output is ever "
                "written back into the clinical database."
            ),
            deliverables=("Data flow diagram", "Source data agreement / source "
                          "data location log", "Data Transfer Agreements"),
            responsible="Data manager",
            timing="Agreed before database go-live",
            reference="ICH E6(R2) 4.9, 5.5.3",
        ),
        DMPSection(
            key="database_build_uat",
            title="Database build, validation and user acceptance testing",
            content=(
                "The database is built from this CRF specification and the edit "
                "check specification. Validation follows a written test plan: "
                "every field is tested against its permitted range and "
                "codelist, and every edit check is tested with at least one "
                "case that must fire and one that must not, including the "
                "boundary values. User acceptance testing is performed by data "
                "management and by a site user against scripted scenarios and "
                "is documented with screenshots and signed test evidence. The "
                "database is not released to production until every test script "
                "has passed and the release is authorised in writing. Any "
                "post-go-live change to a field, codelist or edit check follows "
                "the same change-control path - specification, build, "
                "regression test, authorised release - and the change is "
                "recorded in the audit trail."
            ),
            deliverables=("Edit check specification", "Validation test plan and "
                          "executed scripts", "UAT sign-off", "Release "
                          "authorisation", "Change control log"),
            responsible="Clinical database programmer and data manager",
            timing="Complete before first subject first visit",
            reference="21 CFR Part 11.10(a); ICH E6(R2) 5.5.3",
        ),
        DMPSection(
            key="external_data",
            title="External data transfers",
            content=(
                "Each external data source is governed by a Data Transfer "
                "Agreement fixing the file format, the variable names and "
                "types, the transfer frequency, the transfer medium and its "
                "security, the identifiers on which the data will be joined, "
                "and the named contacts on both sides. A test transfer is "
                "completed and reconciled before the first production transfer. "
                "Sources for this study: "
                + "; ".join(external_sources) + ". " + lloq_text +
                " Every transfer is reconciled on receipt: record counts, "
                "subject and sample identifiers, and duplicates. Unmatched "
                "records are queried to the originating laboratory and are "
                "never silently dropped."
            ),
            deliverables=("Data Transfer Agreements", "Test transfer "
                          "reconciliation", "Per-transfer reconciliation logs"),
            responsible="Data manager with each external provider",
            timing="Agreed before the first transfer; reconciled at every "
                   "transfer",
            reference="ICH E6(R2) 5.5.3, 5.5.4",
        ),
        DMPSection(
            key="medical_coding",
            title="Medical coding",
            content=(
                "Adverse events and medical history are coded to MedDRA; prior "
                "and concomitant medications are coded to WHODrug Global. "
                "Auto-coding runs against the fixed dictionary version; terms "
                "that do not auto-code are coded manually by a qualified coder "
                "and reviewed. A coding review is performed by the medical "
                "monitor before database lock, and the approved coding is "
                "frozen at lock. Verbatim terms are never edited to make them "
                "code - an ambiguous term is queried to the site, because "
                "changing a verbatim term to fit a dictionary entry changes the "
                "clinical record."
            ),
            deliverables=("Coding conventions document", "Coding review "
                          "listings", "Medical monitor coding approval"),
            responsible="Medical coder; approved by the medical monitor",
            timing="Continuous; final approval before database lock",
            reference="ICH E2A; ICH E3 12.2",
        ),
        DMPSection(
            key="query_management",
            title="Query management",
            content=(
                "Queries arise automatically from the programmed edit checks, "
                "from manual data review, and from monitoring. Every query "
                "carries the subject, the form, the field, the question and the "
                "raising party, and it stays in the audit trail after closure. "
                f"The site response target is {QUERY_RESPONSE_TARGET_DAYS} "
                "working days; ageing queries are escalated to the monitor and "
                "then to the investigator. A query is closed only by data "
                "management after the site has answered it and, where "
                "necessary, corrected the data - a site cannot close its own "
                "query. Data management never changes a clinical value on the "
                "site's behalf; self-evident corrections are limited to a "
                "pre-agreed, documented list (for example obvious unit "
                "conversions) and each one is recorded in the audit trail."
            ),
            deliverables=("Query listings and ageing reports", "Self-evident "
                          "correction list"),
            responsible="Data manager",
            timing="Continuous; all queries closed before database lock",
            reference="ICH E6(R2) 4.9.3, 5.5.3",
        ),
        DMPSection(
            key="sae_reconciliation",
            title="SAE reconciliation between the clinical and safety databases",
            content=(
                "Serious adverse events exist in two systems: the clinical "
                "database (the AE form) and the pharmacovigilance safety "
                "database. They are reconciled on the safety database reference "
                "number and the subject identifier, comparing the verbatim "
                "term and its coded Preferred Term, the onset and resolution "
                "dates, the seriousness criteria, the severity, the "
                "investigator causality, the action taken and the outcome. "
                f"Reconciliation runs on receipt of each new serious event and "
                f"at least every {SAE_RECONCILIATION_INTERVAL_DAYS} days, with "
                "a final reconciliation that must be complete and signed before "
                "database lock. Discrepancies are resolved at source with the "
                "investigator, not by editing one system to match the other. "
                "An event present in only one system is treated as a finding in "
                "its own right: a serious event in the clinical database but "
                "not in the safety database means an expedited reporting "
                "obligation may have been missed, and the reporting timelines "
                "themselves are those specified in the safety reporting section "
                "of the protocol."
            ),
            deliverables=("Reconciliation listings", "Discrepancy log and "
                          "resolutions", "Signed final reconciliation"),
            responsible="Data manager and the pharmacovigilance function",
            timing=(f"On receipt and at least every "
                    f"{SAE_RECONCILIATION_INTERVAL_DAYS} days; final pass "
                    f"before lock"),
            reference="ICH E2A; ICH E6(R2) 5.5.3, 5.17",
        ),
        DMPSection(
            key="deviation_and_population_review",
            title="Protocol deviation review and analysis population assignment",
            content=(
                "Protocol deviations are recorded as they occur and classified "
                "as major or minor at a blinded data review meeting attended by "
                "the sponsor, the medical monitor, the data manager and the "
                "biostatistician. The same meeting assigns the analysis "
                "populations and records every exclusion with its reason. The "
                "meeting takes place after all data are clean and before the "
                "treatment codes are released, so that no exclusion decision "
                "can be influenced by knowing which product a subject received. "
                "Its minutes are signed and become an essential document."
            ),
            deliverables=("Protocol deviation listing with classifications",
                          "Blinded data review minutes",
                          "Signed analysis population assignments"),
            responsible="Sponsor, medical monitor, biostatistician, data manager",
            timing="After data cleaning and before unblinding and database lock",
            reference="ICH E9 5.2; ICH E3 10.1, 16.2",
        ),
        DMPSection(
            key="database_lock",
            title="Database lock",
            content=(
                "Lock is authorised only when all of the following are "
                "documented as complete: all CRF pages entered and monitored; "
                "all queries closed; all external data received, reconciled and "
                "loaded; medical coding complete and approved; SAE "
                "reconciliation complete and signed; protocol deviations "
                "classified; analysis populations assigned at the blinded data "
                "review; and the final data review listings reviewed and signed. "
                "The lock is authorised by signature of the sponsor, the data "
                "manager, the biostatistician and the medical monitor. On lock, "
                "write access is withdrawn for all users. Any post-lock change "
                "requires a documented unlock request stating the change and "
                "its justification, the same signatories' approval, an audit-"
                "trailed change and a re-lock; the biostatistician assesses "
                "whether any analysis must be re-run. Unlocking is an "
                "exception, not a routine step."
            ),
            deliverables=("Lock checklist", "Signed lock authorisation",
                          "Any unlock request, approval and re-lock record"),
            responsible="Sponsor, data manager, biostatistician, medical monitor",
            timing="After all lock criteria are met; before unblinding for "
                   "analysis",
            reference="ICH E6(R2) 5.5.3; ICH E9 5.1",
        ),
        DMPSection(
            key="audit_trail_and_part11",
            title="Audit trail, access control and electronic signatures",
            content=(
                "The clinical database maintains a secure, computer-generated, "
                "time-stamped audit trail that independently records the "
                "operator, the date and time, the old value, the new value and "
                "the reason for every creation, modification or deletion of an "
                "electronic record. The audit trail cannot be disabled or "
                "edited by any user, including administrators; it is retained "
                "for at least as long as the records themselves and is "
                "available for review and copying by inspectors. Access is "
                "role-based and limited to authorised individuals with unique "
                "user accounts that are never shared; accounts are removed "
                "promptly when a person leaves the study. System clocks are "
                "synchronised to a reference time source, which is what makes "
                "the recorded dose and draw times comparable across "
                "workstations. Electronic signatures carry the signer's printed "
                "name, the date and time, and the meaning of the signature, and "
                "are linked to their records so that they cannot be excised, "
                "copied or transferred. The system holds current validation "
                "documentation."
            ),
            deliverables=("System validation documentation", "User access log",
                          "Audit trail export at lock",
                          "Electronic signature manifestations"),
            responsible="Sponsor/CRO system owner and data manager",
            timing="In force from database go-live through archival",
            reference=("21 CFR Part 11.10(d), 11.10(e), 11.50, 11.70; EU GMP "
                       "Annex 11; FDA guidance on electronic systems, records "
                       "and signatures in clinical investigations"),
        ),
        DMPSection(
            key="data_privacy",
            title="Subject confidentiality and data protection",
            content=(
                "Subjects are identified in the database only by the subject "
                "identifier; names and other direct identifiers remain at the "
                "site on the subject identification log, which is not "
                "transferred to the sponsor. Where the date of birth is "
                "restricted by local law, only age is collected. Transfers are "
                "encrypted in transit and at rest. The consent form states the "
                "purposes for which data will be used, who will have access, "
                "and the retention period, and the sponsor and its "
                "representatives, the IRB/IEC and regulatory authorities are "
                "granted direct access to source records for verification."
            ),
            deliverables=("Subject identification log (retained at site)",
                          "Data protection impact assessment where required"),
            responsible="Investigator and sponsor",
            timing="Throughout the study and the retention period",
            reference="ICH E6(R2) 2.11, 4.8.10(o), 6.10; applicable data "
                      "protection law",
        ),
        DMPSection(
            key="archival",
            title="Data retention and archival",
            content=(
                "At lock the following are archived: the final clinical "
                f"database in an open, non-proprietary format ({SDTM_IG_VERSION} "
                f"tabulation datasets with {DEFINE_XML_VERSION} metadata and an "
                "annotated CRF), the eCRF casebooks as a human-readable PDF per "
                "subject, the complete audit trail export, the query history, "
                "the coding dictionaries and versions used, the edit check "
                "specification and validation evidence, this plan and all its "
                "versions, and the lock authorisation. Archived records are "
                "readable without the original application. The archive is "
                f"retained for at least {ARCHIVE_RETENTION_YEARS} years after "
                "the end of the trial, or longer where a marketing application "
                "or local law requires it, and nothing is destroyed without "
                "prior written sponsor authorisation. The site retains its own "
                "essential documents for the same period and notifies the "
                "sponsor if it can no longer do so."
            ),
            deliverables=("Archive index", "Readable-media verification record",
                          "Retention and destruction authorisation policy"),
            responsible="Sponsor and investigator, each for their own records",
            timing="At database lock and study close-out",
            reference=("ICH E6(R2) 4.9.5, 5.5.11, 8; 21 CFR 312.62(c); EU "
                       "Regulation 536/2014 Art. 58 (25-year clinical trial "
                       "master file retention)"),
        ),
    )


def build_data_management_plan(
    protocol_context: Optional[dict[str, Any]] = None,
    *,
    design_key: Optional[str] = None,
    periods: Optional[int] = None,
    n_enrolled: Optional[int] = None,
    active_conditions: Optional[set] = None,
    lloq: Optional[float] = None,
) -> dict[str, Any]:
    """Build the Data Management Plan for THIS study.

    Kept separately callable so the DMP can be regenerated (for example after a
    design amendment) without rebuilding the whole CRF specification.
    """
    ctx = protocol_context or {}
    ss = _dig(ctx, "sample_size", "_sample_size") or {}
    design = (design_key or ss.get("design_key")
              or _dig(ctx, "design") or "crossover").strip().lower()
    sampling = _dig(ctx, "sampling_schedule", "_sampling_schedule")
    n_periods = periods or _periods_for(design, sampling)
    n = n_enrolled if n_enrolled is not None else ss.get("n_enrolled")
    active = set(active_conditions or ())
    the_lloq = lloq if lloq is not None else _dig(ctx, "lloq")

    sections = _dmp_sections(
        design=design, periods=n_periods, n_enrolled=n, active=active,
        lloq=the_lloq,
    )
    return {
        "design_key": design,
        "periods": n_periods,
        "sections": [s.to_dict() for s in sections],
        "coding_dictionaries": [dict(d) for d in _CODING_DICTIONARIES],
        "standards": {
            "collection": CDASH_IG_VERSION,
            "tabulation": SDTM_IG_VERSION,
            "metadata": DEFINE_XML_VERSION,
            "controlled_terminology": CDISC_CT_POLICY,
        },
        "retention_years": ARCHIVE_RETENTION_YEARS,
        "query_response_target_days": QUERY_RESPONSE_TARGET_DAYS,
        "sae_reconciliation_interval_days": SAE_RECONCILIATION_INTERVAL_DAYS,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_crf_spec(
    protocol_context: Optional[dict[str, Any]] = None,
    *,
    design_key: Optional[str] = None,
    study_type: Optional[str] = None,
    sampling: Optional[dict[str, Any]] = None,
    label: Optional[dict[str, str]] = None,
    dosing: Optional[str] = None,
    is_biologic: Optional[bool] = None,
    n_enrolled: Optional[int] = None,
    t_half_hours: Optional[float] = None,
    lloq: Optional[float] = None,
    pd_endpoint: Optional[str] = None,
) -> dict[str, Any]:
    """Build the CRF specification and Data Management Plan for THIS study.

    Parameters
    ----------
    protocol_context:
        The generation context assembled for the protocol (or the stored
        ``population_config``). Everything below is read from it when present;
        the explicit keywords exist so the engine can be driven directly and so
        a caller can override a single input without rebuilding the context.
    design_key:
        'crossover' | 'parallel' | 'replicate'. Decides the number of periods,
        the sequence codelist, whether dosing/PK/disposition forms repeat per
        period, and whether a washout interval exists at all.
    study_type:
        'BE' | 'food_effect' | 'biosimilar_pk'. A food-effect study collects the
        standardised meal as data; a biosimilar PK study always carries the
        immunogenicity log.
    sampling:
        The output of :func:`services.sampling_schedule.compute_sampling_schedule`.
        When supplied, the PK sample log is pre-printed with the actual nominal
        timepoints and their permitted windows, and the visit structure splits
        in-house from ambulatory draws. When absent, the specification says so
        and raises a flag rather than inventing a timepoint list.
    label:
        The trimmed DailyMed label dict already assembled for the protocol. Its
        warnings and pharmacology text drive which conditional forms, fields and
        edit checks are switched on.
    dosing / is_biologic / n_enrolled / t_half_hours / lloq / pd_endpoint:
        Study attributes read from the context when not passed explicitly.

    Returns
    -------
    A JSON-serialisable dict (it is persisted inside ``population_config``, a
    JSONB column) holding the forms and their fields, the visit structure, the
    programmed edit checks, the downstream-analysis coverage map, the Data
    Management Plan, the assumptions relied on and the flags the protocol text
    must surface.
    """
    ctx = protocol_context or {}
    ss = _dig(ctx, "sample_size", "_sample_size") or {}

    design = (design_key or ss.get("design_key") or _dig(ctx, "design")
              or "crossover").strip().lower()
    study = (study_type or _dig(ctx, "study_type") or "BE").strip()
    sched = sampling if sampling is not None else _dig(
        ctx, "sampling_schedule", "_sampling_schedule")
    the_label = label if label is not None else _dig(ctx, "label_data", "_label")
    dose_mode = (dosing or _dig(ctx, "dosing") or "single").strip().lower()
    multiple_dose = dose_mode in ("multiple", "multi", "repeat")
    biologic = bool(is_biologic) if is_biologic is not None else bool(
        _dig(ctx, "is_biologic") or study == "biosimilar_pk"
        or (sched or {}).get("is_biologic"))
    n = n_enrolled if n_enrolled is not None else ss.get("n_enrolled")
    t_half = (t_half_hours if t_half_hours is not None
              else ss.get("t_half_hours") or (sched or {}).get("t_half_hours"))
    the_lloq = lloq if lloq is not None else _dig(ctx, "lloq")
    pd_value = pd_endpoint if pd_endpoint is not None else _decision_value(
        ctx, "pd_endpoint")

    assumptions: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []

    # -- Which conditional requirements are switched on --------------------
    detected = _detect(_label_text(the_label))
    active: set[str] = set(detected)
    periods = _periods_for(design, sched)
    if periods > 1:
        active.add("multi_period")
    if study == "food_effect":
        active.add("food_effect")
    if multiple_dose:
        active.add("multiple_dose")
    if biologic:
        active.add("biologic")
        # A biosimilar PK study always carries an immunogenicity log, whether or
        # not the reference label happens to use the word "immunogenicity".
        active.add("immunogenicity")
    if _has_pd_coprimary(pd_value):
        active.add("pd_coprimary")

    sequences = _sequences_for(design, periods)
    washout = _washout_days(t_half, design)

    # -- Instantiate the forms --------------------------------------------
    forms: list[dict[str, Any]] = []
    considered: list[dict[str, Any]] = []
    for form in _CATALOGUE:
        applies = (not form.conditional_on) or (form.conditional_on in active)
        d = form.to_dict(active)
        d["applies"] = applies
        d["condition_rationale"] = (
            _CONDITION_RATIONALE.get(form.conditional_on, "")
            if form.conditional_on else "")
        d["detected_from_label"] = form.conditional_on in detected
        d["repeats"] = (periods if form.repeating == "per_period" else None)
        considered.append(d)
        if applies:
            forms.append(d)

    # Substitute the study's real sequence codelist into the demographics form.
    for form in forms:
        if form["key"] != "dm_demographics":
            continue
        for field in form["fields"]:
            if field["name"] == "sequence":
                field["codelist"] = list(sequences)
                field["permitted"] = "One of: " + " | ".join(sequences)

    # -- Pre-print the PK sample log from the locked sampling schedule -----
    pk_rows: list[dict[str, Any]] = []
    if sched and sched.get("timepoints"):
        for tp in sched["timepoints"]:
            pk_rows.append({
                "index": tp.get("index"),
                "nominal_time_h": tp.get("hours"),
                "label": tp.get("label"),
                "phase": tp.get("phase"),
                "window": tp.get("window"),
                "window_minutes": tp.get("window_minutes"),
                "volume_ml": tp.get("volume_ml"),
                "lambda_z_point": tp.get("lambda_z_point"),
                "repeated_in_periods": periods,
            })
    else:
        flags.append({
            "key": "sampling_schedule_not_supplied",
            "severity": "warning",
            "message": (
                "No computed sampling schedule was supplied, so the PK sample "
                "log could not be pre-printed with this study's nominal "
                "timepoints. The form structure below is correct, but the "
                "timepoint rows must be generated from the locked sampling "
                "schedule before the database is built - a PK log with "
                "free-typed timepoints is how nominal-time-only data gets "
                "created."
            ),
            "must_surface_in_protocol": True,
        })

    if the_lloq is None:
        flags.append({
            "key": "lloq_not_specified",
            "severity": "warning",
            "message": (
                "No lower limit of quantification was supplied. The Data "
                "Transfer Agreement with the bioanalytical laboratory must fix "
                "it before the first transfer, because the analysis treats "
                "values below it as missing rather than zero and cannot apply "
                "that rule without the number."
            ),
            "must_surface_in_protocol": True,
        })

    if periods > 1 and washout is None:
        flags.append({
            "key": "washout_unknown",
            "severity": "warning",
            "message": (
                "The between-period washout could not be derived because no "
                "terminal half-life was available. The schedule of assessments "
                "must state the washout locked in the study design section "
                "before the CRF visit structure is finalised."
            ),
            "must_surface_in_protocol": True,
        })

    # -- Assumptions the protocol has to disclose --------------------------
    assumptions.extend([
        {"key": "screening_window_days", "value": SCREENING_WINDOW_DAYS,
         "basis": ("Conventional healthy-volunteer screening window: long enough "
                   "for laboratory turnaround and re-tests, short enough that "
                   "screening results still represent the subject at dosing.")},
        {"key": "confinement_discharge_hours", "value": CONFINEMENT_DISCHARGE_HOURS,
         "basis": ("Subjects remain in-house through the 24 h draw and return as "
                   "outpatients for later timepoints. This is a scheduling "
                   "convention, not a regulatory requirement, and it decides "
                   "which draws are ambulatory.")},
        {"key": "follow_up_days_after_last_dose",
         "value": FOLLOW_UP_DAYS_AFTER_LAST_DOSE,
         "basis": (f"Safety follow-up {FOLLOW_UP_DAYS_AFTER_LAST_DOSE} (+/- "
                   f"{FOLLOW_UP_WINDOW_DAYS}) days after the last dose, the "
                   f"conventional interval for a single-dose healthy-volunteer "
                   f"study.")},
        {"key": "time_recording_precision_min", "value": TIME_RECORDING_PRECISION_MIN,
         "basis": ("Dose and blood-draw clock times are recorded to the minute "
                   "from a synchronised site clock. Rounding to 5 minutes is "
                   "already a 4% timing error on an early absorption sample of a "
                   "fast-absorbed drug.")},
        {"key": "max_time_to_centrifuge_min", "value": MAX_TIME_TO_CENTRIFUGE_MIN,
         "basis": ("Whole blood is centrifuged within 30 minutes of collection. "
                   "This must be reconciled with the validated bioanalytical "
                   "method's own whole-blood stability data before the study "
                   "starts.")},
        {"key": "sdtm_ig_version", "value": SDTM_IG_VERSION,
         "basis": ("Submission tabulation standard assumed for the annotated CRF "
                   "and the define.xml; must be re-confirmed against the FDA "
                   "Data Standards Catalog in force at submission.")},
        {"key": "coding_dictionary_version_policy",
         "value": "Fixed at database build; whole-study re-code if upversioned",
         "basis": (f"MedDRA is released {MEDDRA_RELEASE_CADENCE} and WHODrug "
                   f"{WHODRUG_RELEASE_CADENCE}; a partial re-code would make the "
                   f"adverse event frequency table internally inconsistent.")},
    ])
    if washout is not None:
        assumptions.append({
            "key": "washout_days", "value": washout,
            "basis": (f"{WASHOUT_HALF_LIVES:.0f} x terminal t1/2 - the same "
                      f"figure locked in the study design section, reused here "
                      f"rather than re-derived so the schedule of assessments "
                      f"cannot disagree with it."),
        })

    # -- Assemble ----------------------------------------------------------
    edit_checks = _build_edit_checks(
        active=active, periods=periods, sampling=sched, sequences=sequences)
    coverage = _analysis_coverage(forms)
    if not coverage["is_superset"]:
        flags.append({
            "key": "analysis_columns_uncovered",
            "severity": "critical",
            "message": (
                "The CRF does not reach every column the downstream analysis "
                "requires: " + ", ".join(coverage["uncovered_required_columns"]) +
                ". A study run on this specification could not be analysed by "
                "the non-compartmental and bioequivalence engines without "
                "collecting those data some other way."
            ),
            "must_surface_in_protocol": True,
        })

    visits = _build_visit_schedule(
        periods=periods, design=design, sampling=sched, washout_days=washout,
        active=active)

    dmp = build_data_management_plan(
        ctx, design_key=design, periods=periods, n_enrolled=n,
        active_conditions=active, lloq=the_lloq)

    result: dict[str, Any] = {
        "design_key": design,
        "study_type": study,
        "periods": periods,
        "sequences": list(sequences),
        "dosing": "multiple" if multiple_dose else "single",
        "is_biologic": biologic,
        "n_enrolled": n,
        "washout_days": washout,
        "active_conditions": sorted(active),
        "detected_from_label": sorted(detected),
        "condition_rationale": {
            k: _CONDITION_RATIONALE.get(k, "") for k in sorted(active)
        },
        "forms": forms,
        "considered": considered,
        "n_forms": len(forms),
        "n_fields": sum(f["n_fields"] for f in forms),
        "critical_forms": [f["key"] for f in forms if f["critical"]],
        "visit_schedule": visits,
        "pk_sample_log_rows": pk_rows,
        "edit_checks": edit_checks,
        "n_edit_checks": len(edit_checks),
        "edit_check_counts": {
            t: sum(1 for c in edit_checks if c["check_type"] == t)
            for t in ("range", "cross_form", "missing", "consistency",
                      "uniqueness", "reconciliation")
        },
        "downstream_analysis_coverage": coverage,
        "standards": {
            "collection": CDASH_IG_VERSION,
            "tabulation": SDTM_IG_VERSION,
            "metadata": DEFINE_XML_VERSION,
            "controlled_terminology": CDISC_CT_POLICY,
        },
        "sdtm_domains": sorted({f["sdtm_domain"] for f in forms if f["sdtm_domain"]}),
        "data_management_plan": dmp,
        "assumptions": assumptions,
        "flags": flags,
        "must_disclose": any(f.get("must_surface_in_protocol") for f in flags),
    }
    result["summary"] = _summary(result)
    return result


def build_crf_dmp(
    protocol_context: Optional[dict[str, Any]] = None, **kwargs: Any
) -> dict[str, Any]:
    """Alias for :func:`build_crf_spec`, named for the pair it produces.

    The CRF specification and the DMP are one artifact in practice - the plan
    describes how the data the CRF defines are managed - so they are built and
    persisted together.
    """
    return build_crf_spec(protocol_context, **kwargs)


def _summary(r: dict[str, Any]) -> str:
    """One-paragraph plain statement for the protocol narrative."""
    periods = r["periods"]
    pk_rows = len(r["pk_sample_log_rows"])
    pk_text = (
        f"The PK sample log is pre-printed with the {pk_rows} nominal timepoints "
        f"of the locked sampling schedule, repeated in "
        f"{'each of the ' + str(periods) + ' periods' if periods > 1 else 'the single period'}, "
        f"so a site selects a scheduled row rather than typing a time"
        if pk_rows else
        "The PK sample log must be pre-printed from the locked sampling schedule "
        "before the database is built"
    )
    return (
        f"Data are collected on {r['n_forms']} electronic case report forms "
        f"({r['n_fields']} fields) mapped to the {', '.join(r['sdtm_domains'])} "
        f"SDTM domains, validated by {r['n_edit_checks']} programmed edit checks. "
        f"{pk_text}. Both the actual clock time of dosing and the actual clock "
        f"time of every blood draw are recorded to the minute, and the elapsed "
        f"time used by the pharmacokinetic analysis is derived from them by the "
        f"database rather than assumed from the nominal time; a draw that "
        f"precedes its dose cannot be saved. Adverse events and medical history "
        f"are coded to MedDRA and medications to WHODrug, serious events are "
        f"reconciled against the safety database before lock, and the database "
        f"is locked only after all queries are closed, coding is approved and "
        f"the analysis populations have been assigned at a blinded data review. "
        f"All electronic records carry a Part 11 audit trail and are archived "
        f"for at least {ARCHIVE_RETENTION_YEARS} years."
    )


def _protocol_scope_directive(r: dict[str, Any]) -> str:
    """Render only the part of the specification that belongs in a PROTOCOL.

    The protocol says that data are captured on a case report form and managed
    under a Data Management Plan, and it states the handful of conventions a
    reader has to know to interpret the study: the actual-versus-nominal time
    rule, the coding dictionaries, the query and lock discipline, the audit
    trail and the retention period.

    Everything that makes this engine large - the per-form field tables, the
    permitted values, the CDISC variable mapping, the pre-printed PK sample log
    rows and the edit-check catalogue - is NOT rendered here. It is still
    computed and still available on the result (and through ``scope="crf"`` /
    ``"sap"``) for the separate CRF specification and Statistical Analysis Plan
    document. The prohibition is written out explicitly rather than merely
    omitted, because a model handed a rich context will otherwise reconstruct
    the tables from the counts and the domain list.
    """
    dmp = r.get("data_management_plan") or {}
    standards = r.get("standards") or {}
    retention = dmp.get("retention_years") or ARCHIVE_RETENTION_YEARS
    query_days = (dmp.get("query_response_target_days")
                  or QUERY_RESPONSE_TARGET_DAYS)
    sae_days = (dmp.get("sae_reconciliation_interval_days")
                or SAE_RECONCILIATION_INTERVAL_DAYS)

    lines = [
        "CASE REPORT FORM AND DATA MANAGEMENT - PROTOCOL SCOPE. These facts are "
        "ALREADY DETERMINED. Narrate ONLY the statements listed below. The "
        "field-level case report form specification is a SEPARATE document and "
        "is deliberately not supplied here.",
        "DO NOT WRITE INTO THE PROTOCOL - the following belong to the separate "
        "Case Report Form Specification / Statistical Analysis Plan package:",
        "  - any 'Critical Fields' list or field-level specification for any "
        "form, in particular Study Drug Administration (Dosing Log) (EX "
        "domain), PK Blood Sample Collection Log (PC domain) and Adverse "
        "Events (AE domain);",
        "  - any per-form or per-domain table of fields, data types, units, "
        "permitted values, codelists, CDISC variable names or required flags;",
        "  - the pre-printed PK Sample Log rows (one row per nominal timepoint "
        "per period) and their per-row collection windows;",
        "  - the programmed edit-check catalogue and the form-by-visit matrix.",
        "  Do not abridge them either: no partial table, no example row, no "
        "'selected critical fields', no annotated form layout. The sampling "
        "timepoints and their windows are stated ONCE, in the study "
        "assessments section, from the locked sampling schedule; they are not "
        "restated as a case report form log.",
        "STATE THIS, AND NOTHING FURTHER ABOUT THE COLLECTION INSTRUMENT:",
    ]
    if r.get("summary"):
        lines.append(f"  - {r['summary']}")
    lines.extend([
        "  - Study data are recorded by site staff on validated electronic case "
        "report forms and are managed under a study-specific Data Management "
        "Plan. The case report form specification, its field definitions, its "
        "permitted values and the programmed edit checks are maintained as "
        "separate controlled documents and are not reproduced in this protocol.",
        "  - The actual clock time of dosing and the actual clock time of every "
        "pharmacokinetic blood draw are recorded to the minute from a "
        "synchronised site clock. The pharmacokinetic analysis uses the elapsed "
        "time derived from those two times; nominal times are used for "
        "presentation only.",
        "  - Adverse events and medical history are coded to MedDRA, and prior "
        "and concomitant medications to WHODrug Global, each at a single "
        "dictionary version fixed at database build.",
        f"  - Queries are raised from the programmed validation checks and from "
        f"data review, with a site response target of {query_days} working "
        f"days; a query is closed by data management after the site has "
        f"answered it, never by the site alone.",
        f"  - Serious adverse events are reconciled between the clinical "
        f"database and the sponsor's safety database on receipt and at least "
        f"every {sae_days} days, with a final reconciliation completed and "
        f"signed before database lock.",
        "  - The database is locked only after all case report form data are "
        "entered and monitored, all queries are closed, external data are "
        "received and reconciled, medical coding is approved, protocol "
        "deviations are classified and the analysis populations are assigned "
        "at a blinded data review. Any post-lock change requires a documented "
        "unlock authorised jointly by the sponsor and the data manager, an "
        "audit-trailed correction and a re-lock.",
        "  - All electronic records carry a secure, computer-generated, "
        "time-stamped audit trail that records the operator, the old value and "
        "the new value and cannot be disabled, with role-based access under "
        "unique, unshared user accounts (21 CFR Part 11).",
        f"  - Records are retained for at least {retention} years after the end "
        f"of the trial, or longer where a marketing application or local law "
        f"requires it, and nothing is destroyed without prior written sponsor "
        f"authorisation.",
        "  - Source records remain at the site; the sponsor and its "
        "representatives, the IRB/IEC and regulatory authorities are granted "
        "direct access to them for verification.",
    ])
    if standards:
        lines.append(
            f"  - Data standards: {standards.get('collection', '')} for "
            f"collection, {standards.get('tabulation', '')} for submission "
            f"tabulation and {standards.get('metadata', '')} for metadata. The "
            f"version in force at database lock governs and is re-confirmed "
            f"against the regulatory data-standards catalogue current at "
            f"submission."
        )

    titles = [s.get("title") for s in dmp.get("sections", []) if s.get("title")]
    if titles:
        lines.append(
            "The Data Management Plan is a separate controlled document. Refer "
            "to it by name; do not reproduce its content. It covers: "
            + "; ".join(titles) + "."
        )

    # Deliberately NOT carried into the protocol: the downstream analysis
    # coverage statement. It names this product's own internal modules, which is
    # engineering provenance, not protocol text.
    for a in r.get("assumptions", []):
        lines.append(
            f"ASSUMPTION to disclose as a pre-specified convention, not as a "
            f"regulatory requirement - {a['key']} = {a['value']}: {a['basis']}"
        )
    for f in r.get("flags", []):
        if f.get("must_surface_in_protocol"):
            lines.append(
                f"MUST STATE IN THE PROTOCOL ({f['severity']}): {f['message']}")
    return "\n".join(lines)


def crf_dmp_directive(result: dict[str, Any], scope: str = "protocol") -> str:
    """Render the locked CRF/DMP facts as prompt text for the generator.

    Mirrors ``stratification.stratification_directive`` and
    ``sampling_schedule.sampling_schedule_directive``: the model narrates these
    facts and may not add a form, drop a field, invent a codelist value, or
    soften an edit check.

    ``scope`` exists because this engine emits far more locked content than the
    other engines do, and because most of that content belongs to a document
    that is NOT the protocol:

      * ``"protocol"`` - the default. A brief statement that data are captured
        on a case report form and managed under a Data Management Plan, plus the
        conventions a protocol reader needs (actual-versus-nominal times, coding
        dictionaries, query and lock discipline, audit trail, retention), plus an
        explicit instruction not to render the field-level tables or the
        pre-printed sample-log rows. This is what the protocol quotes.
      * ``"crf"`` (alias ``"sap"``) - forms, field definitions, the pre-printed
        PK log, the visit structure and the edit checks: the full collection
        instrument, for the separate CRF specification / SAP document.
      * ``"dmp"``  - the Data Management Plan sections and the coding
        dictionaries in full.
      * ``"all"``  - both halves in full.

    An unrecognised scope falls back to ``"protocol"``: a typo must not silently
    dump the whole specification into a protocol.

    In the non-protocol scopes, field-level detail is limited to the CRITICAL
    forms and edit-check rationale to the hard-severity checks, so the block
    stays proportionate to the document it is injected into.
    """
    if not result:
        return ""
    # Accept either the combined result or a bare {"crf": ...} wrapper.
    r = result.get("crf") if isinstance(result.get("crf"), dict) else result
    if not r or not r.get("forms"):
        return ""
    key = (scope or "").strip().lower()
    if key == "sap":
        # The SAP/CRF-specification document is exactly the collection
        # instrument, so it is the same rendering under the name the reviewing
        # pharmacologist used for the destination document.
        key = "crf"
    if key not in ("protocol", "crf", "dmp", "all"):
        key = "protocol"
    if key == "protocol":
        return _protocol_scope_directive(r)
    want_crf = key in ("all", "crf")
    want_dmp = key in ("all", "dmp")

    lines = [
        "CASE REPORT FORM SPECIFICATION AND DATA MANAGEMENT PLAN - these are "
        "ALREADY DETERMINED. Narrate them; do not add or remove a form or a "
        "field, do not invent a codelist value, a range or a CDISC domain, and "
        "do not soften or omit an edit check.",
        f"Overview: {r.get('summary', '')}",
        f"Standards: collection {r['standards']['collection']}, tabulation "
        f"{r['standards']['tabulation']}, metadata {r['standards']['metadata']}.",
        f"Design context: {r.get('design_key')} design, {r.get('periods')} "
        f"period(s), sequences {', '.join(r.get('sequences', []))}"
        + (f", washout {_num(r['washout_days'])} days" if r.get("washout_days")
           else "") + ".",
    ]

    if want_crf:
        lines.append("FORMS (title | SDTM domain | when completed | repeat):")
        for f in r["forms"]:
            marker = (" [CRITICAL - data not reconstructable if lost]"
                      if f["critical"] else "")
            lines.append(
                f"  - {f['title']} | {f['sdtm_domain']} | {f['completed_at']} | "
                f"{f['repeating']}{marker}. Purpose: {f['purpose']}"
            )
            if f.get("condition_rationale"):
                lines.append(f"      Included because: {f['condition_rationale']}")

        lines.append(
            "FIELDS ON THE CRITICAL FORMS - reproduce these exactly "
            "(name | type | units | permitted | required):")
        for f in r["forms"]:
            if not f["critical"]:
                continue
            for field in f["fields"]:
                lines.append(
                    f"  - {f['key']}.{field['name']} | {field['dtype']} | "
                    f"{field['units'] or '-'} | {field['permitted']} | "
                    f"{'required' if field['required'] else 'optional'}"
                    + (f" | DERIVED: {field['derivation']}"
                       if field["derived"] else "")
                )

        if r.get("pk_sample_log_rows"):
            lines.append(
                f"PK SAMPLE LOG - one pre-printed row per timepoint per period "
                f"({len(r['pk_sample_log_rows'])} rows x {r['periods']} "
                f"period(s)); reproduce these EXACT nominal times and windows:")
            for row in r["pk_sample_log_rows"]:
                lines.append(
                    f"  {row['index']}. {row['nominal_time_h']} h "
                    f"[{row['label']}] - {row['phase']} phase, "
                    f"window {row['window']}"
                )

        lines.append("VISIT STRUCTURE (visit | timing | forms):")
        for v in r.get("visit_schedule", []):
            lines.append(
                f"  - {v['name']} | {v['timing']} | " + ", ".join(v["forms"])
                + (f" | {v['note']}" if v.get("note") else "")
            )

        lines.append("PROGRAMMED EDIT CHECKS (severity | rule):")
        for c in r.get("edit_checks", []):
            line = f"  - [{c['severity']}] {c['rule']}"
            # Rationale only for the checks that BLOCK an entry: those are the
            # ones the protocol has to justify to a site that will hit them.
            if c["severity"] == "hard":
                line += f" Why: {c['rationale']}"
            lines.append(line)

    dmp = r.get("data_management_plan") or {}
    if want_dmp and dmp.get("sections"):
        lines.append("DATA MANAGEMENT PLAN sections (title | responsible | timing):")
        for s in dmp["sections"]:
            lines.append(
                f"  - {s['title']} | {s['responsible']} | {s['timing']} "
                f"[{s['reference']}]"
            )
            lines.append(f"      {s['content']}")
        for d in dmp.get("coding_dictionaries", []):
            lines.append(
                f"CODING - {d['scope']}: {d['dictionary']}, released "
                f"{d['release_cadence']}. {d['version_policy']}"
            )

    cov = r.get("downstream_analysis_coverage") or {}
    if cov.get("statement"):
        lines.append(f"ANALYSIS DATA CONTRACT: {cov['statement']}")

    for a in r.get("assumptions", []):
        lines.append(f"ASSUMPTION to disclose - {a['key']} = {a['value']}: {a['basis']}")
    for f in r.get("flags", []):
        if f.get("must_surface_in_protocol"):
            lines.append(f"MUST STATE IN THE PROTOCOL ({f['severity']}): {f['message']}")
    return "\n".join(lines)
