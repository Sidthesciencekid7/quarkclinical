"""Study-design decision engine — the five non-negotiable clinical rules that
shape a PK-similarity / bioequivalence study, surfaced (with rationale) to the
"Review design" wizard step where a pharmacologist can override any of them.

The five decisions, in order:

  1. Population — NHV vs patients, gated on safety. Highly immunogenic or
     cytotoxic products are unsafe in healthy volunteers → patients (and
     multi-dose PK for immunogenic products). Some oncology mAbs are safe at a
     single dose in NHV (e.g. trastuzumab) → NHV allowed. A product with
     documented target-mediated drug disposition (TMDD) is studied in patients
     on the labelled multiple-dose regimen, with PK at steady state rather than
     after a single dose.
  2. Dose — therapeutic (label) single dose by default; multiple doses to
     steady state for multi-dose patient studies (always for TMDD products); a
     reduced dose only where safety warrants and no multi-dose regimen applies.
  3. Route / device / site (injectables) — prefer SC when both IV and SC are on
     label (SC earns the IV-PK waiver); prefer PFS over autoinjector; inject in
     the abdomen to minimise injection-site reactions unless the label says
     otherwise.
  4. Design — parallel vs crossover. Reuses the sample-size engine's design_key
     (very short half-life, e.g. insulins → crossover; long-half-life biologics
     → parallel; small-molecule BE → crossover).
  5. PD co-primary — add a PD endpoint as co-primary ONLY when the product has a
     clinically validated PD marker that responds within the study's PK window
     (GIR via euglycemic clamp for insulins, ANC for filgrastim, reticulocytes
     for epoetin). Chronic efficacy endpoints that cannot move in a single-dose
     window (e.g. HbA1c) are deliberately NOT selected here.

Every decision carries its rationale, the evidence it rests on, and the list of
alternatives for the override dropdown. The knowledge base is curated for common
biosimilar / BE archetypes with class-based fallbacks; unknown products get a
conservative default that the reviewer confirms or changes.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional
import copy

POP_HEALTHY = "Healthy volunteers"
POP_PATIENTS = "Patients"
POP_PATIENTS_MULTI = "Patients · multi-dose"
POPULATION_OPTIONS: list[str] = [POP_HEALTHY, POP_PATIENTS, POP_PATIENTS_MULTI]

DOSE_SINGLE = "Therapeutic (label) dose — single dose"
DOSE_MULTIPLE_SS = "Therapeutic (label) dose — multiple doses to steady state"
DOSE_REDUCED = "Reduced dose"
DOSE_OPTIONS: list[str] = [DOSE_SINGLE, DOSE_MULTIPLE_SS, DOSE_REDUCED]

TMDD_VALUES = ("yes", "no", "unknown")


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

# modality: "small_molecule" (oral BE) | "mab" | "insulin" | "protein" | "other"
# immunogenicity: "low" | "moderate" | "high"
# nhv_single_dose_safe: is a single dose safe in healthy volunteers?
# cytotoxic: classical cytotoxic / directly toxic safety profile
# pd_marker: {"name", "method"} if a validated PK-window PD marker exists, else None
# routes: label routes we know ("SC"/"IV"/"oral"), used for the route rule
# tmdd: "yes" = clinically documented target-mediated (saturable, non-linear) clearance;
#       "no" = label states linear PK; "unknown" = not verified (never guessed)
# tmdd_note: citation for the tmdd value ("" for drugs outside the KB)
@dataclass(frozen=True)
class DrugProfile:
    modality: str = "small_molecule"
    immunogenicity: str = "low"
    nhv_single_dose_safe: bool = True
    cytotoxic: bool = False
    pd_marker: Optional[dict[str, str]] = None
    routes: tuple[str, ...] = ()
    tmdd: str = "unknown"
    tmdd_note: str = ""


_INSULIN_PD = {"name": "Glucose infusion rate (GIR)", "method": "euglycemic clamp"}
_ANC_PD = {"name": "Absolute neutrophil count (ANC)", "method": "serial CBC"}
_RETIC_PD = {"name": "Reticulocyte count", "method": "serial CBC"}

# Curated by INN (lowercase). Values reflect the standard regulatory approach for
# each product's biosimilar/BE PK study — the reviewer can override any of them.
#
# tmdd / tmdd_note are checked against the FDA label (Clinical Pharmacology 12.3,
# DailyMed) or published literature. "yes" is asserted ONLY where clearance through
# saturable target binding is documented; "no" only where the label states linear
# PK; everything else is "unknown" (never guessed).
_NV = "not verified"
_SM_NOTE = "Small molecule; not a target-mediated-disposition product"
_KB: dict[str, DrugProfile] = {
    # Insulins → NHV, SC, crossover, GIR co-primary
    "insulin glargine": DrugProfile("insulin", "low", True, False, _INSULIN_PD, ("SC",), tmdd_note=_NV),
    "insulin aspart":   DrugProfile("insulin", "low", True, False, _INSULIN_PD, ("SC",), tmdd_note=_NV),
    "insulin lispro":   DrugProfile("insulin", "low", True, False, _INSULIN_PD, ("SC",), tmdd_note=_NV),
    "insulin detemir":  DrugProfile("insulin", "low", True, False, _INSULIN_PD, ("SC",), tmdd_note=_NV),
    # Myeloid growth factors → NHV, SC, PD = ANC
    # Neupogen (filgrastim) 12.3: "G-CSF receptor-mediated clearance is saturated by high
    # concentration ... and is diminished by neutropenia"; Neulasta 12.3: nonlinear PK,
    # "neutrophil receptor binding is an important component of the clearance".
    "filgrastim":    DrugProfile("protein", "moderate", True, False, _ANC_PD, ("SC", "IV"),
                                 tmdd="yes", tmdd_note="Neupogen label 12.3: nonlinear PK, saturable G-CSF receptor-mediated clearance"),
    "pegfilgrastim": DrugProfile("protein", "moderate", True, False, _ANC_PD, ("SC",),
                                 tmdd="yes", tmdd_note="Neulasta label 12.3: nonlinear PK, neutrophil receptor binding is an important component of clearance"),
    # Lenograstim is not US-labelled; source is the published PK literature
    # (Houston et al., Br J Clin Pharmacol 1999, doi:10.1046/j.1365-2125.1999.00888.x;
    # Hayashi et al., J Clin Pharmacol 1999): dose- and time-dependent PK, clearance rising
    # with neutrophil count via G-CSF receptor uptake.
    "lenograstim":   DrugProfile("protein", "moderate", True, False, _ANC_PD, ("SC", "IV"),
                                 tmdd="yes", tmdd_note="Houston et al., Br J Clin Pharmacol 1999 (doi:10.1046/j.1365-2125.1999.00888.x): dose- and time-dependent PK, neutrophil G-CSF-receptor clearance"),
    # Erythropoiesis-stimulating agents → NHV, PD = reticulocytes
    # Epogen / Aranesp 12.3 do not describe target-mediated clearance → left unverified.
    "epoetin alfa": DrugProfile("protein", "moderate", True, False, _RETIC_PD, ("SC", "IV"), tmdd_note=_NV),
    "darbepoetin alfa": DrugProfile("protein", "moderate", True, False, _RETIC_PD, ("SC", "IV"), tmdd_note=_NV),
    # Targeted mAbs safe single-dose in NHV → NHV, parallel, no PD marker
    # Herceptin / biosimilar labels 12.3: "parallel linear and non-linear elimination pathways".
    "trastuzumab": DrugProfile("mab", "moderate", True, False, None, ("IV", "SC"),
                               tmdd="yes", tmdd_note="Herceptin label 12.3: parallel linear and non-linear elimination pathways (clearance rises as concentration falls)"),
    # Zirabev (bevacizumab) 12.3: "bevacizumab pharmacokinetics are linear" over 1-20 mg/kg.
    "bevacizumab": DrugProfile("mab", "moderate", True, False, None, ("IV",),
                               tmdd="no", tmdd_note="Bevacizumab label 12.3: linear PK over 1-20 mg/kg"),
    # Hyrimoz (adalimumab) 12.3: linear over 0.5-10 mg/kg, no change in clearance over time.
    "adalimumab":  DrugProfile("mab", "high", True, False, None, ("SC",),
                               tmdd="no", tmdd_note="Adalimumab label 12.3: linear PK over 0.5-10 mg/kg IV; trough concentrations proportional to dose"),
    "etanercept":  DrugProfile("protein", "moderate", True, False, None, ("SC",), tmdd_note=_NV),
    # Renflexis (infliximab) 12.3: linear dose-Cmax relationship over 3-20 mg/kg, no accumulation.
    "infliximab":  DrugProfile("mab", "high", True, False, None, ("IV",),
                               tmdd="no", tmdd_note="Infliximab label 12.3: linear dose-Cmax relationship over 3-20 mg/kg, no accumulation"),
    "ranibizumab": DrugProfile("mab", "moderate", True, False, None, ("intravitreal",), tmdd_note=_NV),
    # Omlyclo / Xolair 12.3 (CSU): linear PK across 75-600 mg single SC dose.
    "omalizumab":  DrugProfile("mab", "moderate", True, False, None, ("SC",),
                               tmdd="no", tmdd_note="Omalizumab label 12.3: linear PK across 75-600 mg single SC dose (CSU)"),
    # B-cell depleting / immune-modulating mAbs → NOT NHV-safe → patients
    # Rituxan/Ruxience 12.3: higher CD19+ counts or larger tumour lesions → higher clearance
    # (target-burden-dependent clearance).
    "rituximab":    DrugProfile("mab", "high", False, False, None, ("IV",),
                                tmdd="yes", tmdd_note="Rituximab label 12.3: clearance higher with higher CD19+ cell count / tumour lesion size (CD20 target burden)"),
    # Gazyva 12.3: linear plus time-dependent non-linear clearance, "target-mediated drug
    # disposition (TMDD)" saturating over the treatment cycle.
    "obinutuzumab": DrugProfile("mab", "high", False, False, None, ("IV",),
                                tmdd="yes", tmdd_note="Gazyva label 12.3: linear plus time-dependent non-linear clearance consistent with TMDD"),
    # Checkpoint inhibitors / oncology immunotherapy → patients (immune toxicity)
    # Keytruda / Opdivo 12.3 report dose-proportional exposure and no documented
    # target-mediated non-linearity at labelled doses → left unverified, not asserted.
    "pembrolizumab": DrugProfile("mab", "high", False, True, None, ("IV",), tmdd_note=_NV),
    "nivolumab":     DrugProfile("mab", "high", False, True, None, ("IV",), tmdd_note=_NV),
    # Erbitux 12.3: non-linear PK, greater-than-dose-proportional AUC, clearance falls
    # 0.08 → 0.02 L/h/m2 as dose rises 20 → 200 mg/m2 and plateaus (saturable target clearance).
    "cetuximab":     DrugProfile("mab", "high", False, True, None, ("IV",),
                                 tmdd="yes", tmdd_note="Erbitux label 12.3: non-linear PK, greater than dose-proportional AUC, clearance plateaus at higher doses"),
    # Classical cytotoxics → patients
    "doxorubicin": DrugProfile("small_molecule", "low", False, True, None, ("IV",), tmdd="no", tmdd_note=_SM_NOTE),
    "paclitaxel":  DrugProfile("small_molecule", "low", False, True, None, ("IV",), tmdd="no", tmdd_note=_SM_NOTE),
    # Oral small molecules → NHV, oral, crossover, no PD
    "ibuprofen":    DrugProfile("small_molecule", "low", True, False, None, ("oral",), tmdd="no", tmdd_note=_SM_NOTE),
    "metformin":    DrugProfile("small_molecule", "low", True, False, None, ("oral",), tmdd="no", tmdd_note=_SM_NOTE),
    "atorvastatin": DrugProfile("small_molecule", "low", True, False, None, ("oral",), tmdd="no", tmdd_note=_SM_NOTE),
    "warfarin":     DrugProfile("small_molecule", "low", True, False, None, ("oral",), tmdd="no", tmdd_note=_SM_NOTE),
}


# ---------------------------------------------------------------------------
# Modality classification (biologic vs small molecule) — gates which study
# types (BE / biosimilar_pk / food_effect) are selectable for a given drug.
# Curated KB is authoritative where known; otherwise fall back to WHO INN-stem
# conventions (real regulatory naming, e.g. "-mab" for monoclonal antibodies).
# Unclassifiable drugs return "unknown" so callers do NOT restrict the choice
# rather than risk blocking a legitimate study type on a bad guess.
# ---------------------------------------------------------------------------

_BIOLOGIC_MODALITIES = {"mab", "protein", "insulin"}

# WHO INN stems used for biological products in this app's scope: monoclonal
# antibodies (-mab), fusion proteins (-cept), G-CSF analogues (-grastim),
# erythropoiesis-stimulating agents (-poetin), interleukin/receptor
# antagonists (-kinra), gene therapies (-parvovec), and GLP-1/peptide
# analogues (-tide) which have been regulated as biologics since the 2020
# BPCI Act transition (e.g. dulaglutide, liraglutide, semaglutide).
_BIOLOGIC_INN_STEMS = ("mab", "cept", "grastim", "poetin", "kinra", "parvovec", "tide")
_BIOLOGIC_INN_PREFIXES = ("insulin ", "interferon ")

# Common small-molecule INN stems (kinase/other enzyme inhibitors, antivirals,
# beta blockers, ACE inhibitors, ARBs, statins, PPIs, antibiotics, NSAIDs, etc.)
_SMALL_MOLECULE_INN_STEMS = (
    "nib", "vir", "azole", "olol", "pril", "sartan", "statin", "prazole",
    "cillin", "mycin", "oxacin", "dipine", "profen", "coxib", "gliptin", "gliflozin",
)

StudyModality = str  # "biologic" | "small_molecule" | "unknown"

STUDY_TYPES_BY_MODALITY: dict[str, tuple[str, ...]] = {
    "biologic": ("biosimilar_pk",),
    "small_molecule": ("BE", "food_effect"),
}


def classify_drug_modality(drug_inn: str) -> StudyModality:
    """"biologic" | "small_molecule" | "unknown" for the given INN.

    Checks the curated knowledge base first (authoritative), then WHO INN-stem
    heuristics, else "unknown" — in which case the caller should NOT restrict
    the study-type choice, since we can't confidently classify the product.
    """
    key = (drug_inn or "").strip().lower()
    if key in _KB:
        return "biologic" if _KB[key].modality in _BIOLOGIC_MODALITIES else "small_molecule"
    if any(key.startswith(p) for p in _BIOLOGIC_INN_PREFIXES):
        return "biologic"
    if any(key.endswith(s) for s in _BIOLOGIC_INN_STEMS):
        return "biologic"
    if any(key.endswith(s) for s in _SMALL_MOLECULE_INN_STEMS):
        return "small_molecule"
    return "unknown"


def requires_patient_population(drug_inn: str) -> Optional[bool]:
    """True if the curated safety KB says this product is NOT safe for
    single-dose administration in healthy volunteers (must be studied in
    patients); False if it says the product IS NHV-safe; None if the drug
    isn't in the curated KB (unknown — caller should not infer either way).

    Mirrors the same `not nhv_single_dose_safe` test _population_decision()
    uses for the "Population" design-decision rule, so a drug's healthy-vs-
    patient classification is identical whether it flows through the wizard's
    design-decision engine or the budget engine's subject_profile inference.
    """
    key = (drug_inn or "").strip().lower()
    if key not in _KB:
        return None
    return not _KB[key].nhv_single_dose_safe


# Curated oncology INN list — the checkpoint inhibitors, oncology mAbs, and
# classical cytotoxics already in the KB above. Kept as a plain set (not a
# DrugProfile field) since therapeutic-area classification is a budgeting/
# pricing concern, not a study-design one.
_ONCOLOGY_INN = {
    "pembrolizumab", "nivolumab", "cetuximab", "rituximab", "obinutuzumab",
    "trastuzumab", "bevacizumab", "doxorubicin", "paclitaxel",
}

_ONCOLOGY_INDICATION_KEYWORDS = (
    "cancer", "tumor", "tumour", "oncology", "carcinoma", "lymphoma",
    "leukemia", "leukaemia", "melanoma", "sarcoma", "neoplasm", "malignan",
)


def classify_therapeutic_area(drug_inn: str, indication: Optional[str] = None) -> str:
    """"oncology" | "other" | "unknown" for the given drug/indication, used by
    the budget engine to pick the patient rate-card profile (§1.5 step 4).

    Curated oncology INN list is authoritative; else an oncology-keyword scan
    of the free-text indication; else "other" for any drug we recognise in
    the curated safety KB (a known-but-non-oncology product); else "unknown"
    for a drug we have no information about at all (the caller treats
    "unknown" as "other" for pricing but records that it isn't classifiable).
    """
    key = (drug_inn or "").strip().lower()
    if key in _ONCOLOGY_INN:
        return "oncology"
    if indication:
        low = indication.strip().lower()
        if any(kw in low for kw in _ONCOLOGY_INDICATION_KEYWORDS):
            return "oncology"
    if key in _KB:
        return "other"
    return "unknown"


def allowed_study_types(drug_inn: str) -> Optional[tuple[str, ...]]:
    """Study types selectable for this drug, or None if unrestricted (modality
    unknown). E.g. a biologic (mAb, insulin, therapeutic protein) may only run
    a biosimilar PK-similarity study, never a small-molecule BE or food-effect
    study, and vice versa."""
    modality = classify_drug_modality(drug_inn)
    return STUDY_TYPES_BY_MODALITY.get(modality)


def _profile_for(drug_inn: str, study_type: str, label_routes: tuple[str, ...]) -> DrugProfile:
    """Look up the curated profile; else infer a conservative one from study type
    and any routes we could read off the label."""
    key = (drug_inn or "").strip().lower()
    if key in _KB:
        prof = _KB[key]
        # Prefer real label routes when we actually have them.
        return DrugProfile(prof.modality, prof.immunogenicity, prof.nhv_single_dose_safe,
                           prof.cytotoxic, prof.pd_marker, label_routes or prof.routes,
                           tmdd=prof.tmdd, tmdd_note=prof.tmdd_note)
    # Unknown drug: infer from study type. Biosimilars are biologics (default mAb,
    # NHV-safe single dose, no known PD marker); everything else is treated as an
    # oral small molecule. The reviewer confirms/overrides. TMDD is never guessed.
    if study_type == "biosimilar_pk":
        return DrugProfile("mab", "moderate", True, False, None, label_routes or ("SC", "IV"),
                           tmdd="unknown", tmdd_note="")
    return DrugProfile("small_molecule", "low", True, False, None, label_routes or ("oral",),
                       tmdd="unknown", tmdd_note="")


def drug_tmdd(drug_inn: str) -> tuple[str, str]:
    """(tmdd, tmdd_note) from the curated KB, else ("unknown", "").

    Lets other engines and tests read the curated target-mediated-disposition
    call without reaching into the private knowledge base."""
    key = (drug_inn or "").strip().lower()
    if key in _KB:
        return _KB[key].tmdd, _KB[key].tmdd_note
    return "unknown", ""


# ---------------------------------------------------------------------------
# Live label extraction (route / device / injection site)
# ---------------------------------------------------------------------------
#
# The curated KB above is a starting guess. When the drug's real DailyMed
# "Dosage and Administration" / "How Supplied" text is available (fetched by
# services/literature_service.py, same free NIH API already used for PK
# extraction — no AI cost), these functions read the ACTUAL label instead of
# guessing, and the route decision's `evidence` field says so.


_NEGATION_CUES = ("not ", "no ", "never ", "avoid ", "contraindicated", "should not", "must not", "do not")
_NEGATION_WINDOW = 40  # chars to look back from a route/device mention


def _is_negated(t: str, match_start: int) -> bool:
    """True if a negation cue ('do not administer intravenously') precedes the
    match closely enough to be modifying it — so a label that RULES OUT a route
    doesn't get misread as offering it."""
    preceding = t[max(0, match_start - _NEGATION_WINDOW):match_start]
    return any(cue in preceding for cue in _NEGATION_CUES)


def _first_non_negated_match(t: str, pattern: str) -> bool:
    """True if `pattern` occurs at least once WITHOUT a preceding negation cue —
    i.e. the label affirmatively supports it somewhere, even if it also has an
    unrelated negated mention elsewhere ('may be given SC; do not give IV')."""
    return any(not _is_negated(t, m.start()) for m in re.finditer(pattern, t))


def extract_label_routes(text: str) -> tuple[str, ...]:
    """Routes explicitly and affirmatively stated in the label text (a route
    only ruled OUT by a 'do not administer ...' sentence is excluded), in
    first-mentioned order."""
    t = (text or "").lower()
    routes: list[str] = []
    for name, pattern in (
        ("SC", r"\bsubcutaneous(ly)?\b|\bsc injection\b"),
        ("IV", r"\bintravenous(ly)?\b|\biv infusion\b"),
        ("IM", r"\bintramuscular(ly)?\b"),
        ("intravitreal", r"\bintravitreal(ly)?\b"),
        ("oral", r"\borally\b|\bby mouth\b|\btablet(s)?\b|\bcapsule(s)?\b"),
    ):
        if _first_non_negated_match(t, pattern):
            routes.append(name)
    return tuple(dict.fromkeys(routes))  # de-dup, preserve first-seen order


def extract_label_device(text: str) -> Optional[str]:
    """The label's stated injection device, or None if unstated/ambiguous
    (both PFS and autoinjector mentioned — common when a label covers several
    presentations — defers to the engine's own PFS-preferred default)."""
    t = (text or "").lower()
    has_pfs = bool(re.search(r"pre-?filled syringe", t))
    has_auto = bool(re.search(r"auto-?injector", t))
    if has_pfs and not has_auto:
        return "PFS"
    if has_auto and not has_pfs:
        return "autoinjector"
    return None


def extract_label_site(text: str) -> Optional[str]:
    """The label's stated injection site, or None if unstated/ambiguous
    (multiple sites listed — defers to the engine's abdomen-preferred default)."""
    t = (text or "").lower()
    sites = [site for site, pat in [
        ("abdomen", r"\babdomen\b"),
        ("thigh", r"\bthigh\b"),
        ("upper arm", r"\bupper arm\b"),
    ] if re.search(pat, t)]
    return sites[0] if len(sites) == 1 else None


# ---------------------------------------------------------------------------
# Decision model
# ---------------------------------------------------------------------------


@dataclass
class DesignDecision:
    key: str                       # population | dose | route | design | pd_endpoint
    label: str                     # human label for the wizard
    value: str                     # the chosen value (matches one of `options`)
    rationale: str                 # why this was chosen
    evidence: str                  # what the choice rests on
    options: list[str] = field(default_factory=list)  # override alternatives

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# The five rules
# ---------------------------------------------------------------------------


TMDD_RATIONALE = (
    "The product shows target-mediated drug disposition (TMDD) ({tmdd_note}): part of its "
    "clearance runs through saturable binding to its target, so single-dose PK is dose- and "
    "time-dependent and a less sensitive basis for comparison. PK is therefore evaluated under "
    "the labelled multiple-dose regimen at steady state, where target-mediated clearance is "
    "saturated (steady-state AUC(0-tau), with Cmax,ss and Ctrough), rather than after a single dose."
)


def _tmdd_rationale(prof: DrugProfile) -> str:
    note = prof.tmdd_note if prof.tmdd_note and prof.tmdd_note != "not verified" else "documented target-mediated clearance"
    return TMDD_RATIONALE.format(tmdd_note=note)


def _population_decision(prof: DrugProfile, subject_type: str = "healthy") -> DesignDecision:
    """Population choice. A product with documented TMDD is studied in patients on
    the labelled multiple-dose regimen at steady state (single-dose PK of a
    target-mediated product is non-linear and less sensitive), whether the safety
    gate forces patients or the user asked for patients in the Population step."""
    patient_requested = str(subject_type or "healthy").strip().lower() == "patient"
    tmdd = prof.tmdd == "yes"
    multi = " · multi-dose" if prof.immunogenicity == "high" and not prof.nhv_single_dose_safe else ""
    if not prof.nhv_single_dose_safe:
        if prof.cytotoxic:
            value, rationale = (f"Patients{multi}",
                                "Cytotoxic / toxic safety profile — not safe in healthy volunteers.")
        elif prof.immunogenicity == "high":
            value, rationale = (POP_PATIENTS_MULTI,
                                "Highly immunogenic — repeat-dose PK must be characterised in patients.")
        else:
            value, rationale = (POP_PATIENTS,
                                "Not safe for single-dose administration in healthy volunteers.")
        if tmdd:
            value = POP_PATIENTS_MULTI
            rationale = f"{rationale} {_tmdd_rationale(prof)}"
    elif patient_requested:
        if tmdd:
            value = POP_PATIENTS_MULTI
            rationale = f"Patients were selected in the Population step. {_tmdd_rationale(prof)}"
        else:
            value = POP_PATIENTS
            rationale = ("Patients were selected in the Population step; a single dose would also be "
                         "safe in healthy volunteers, which remains available.")
    else:
        rationale = ("A single dose is safe in healthy volunteers "
                     "(not cytotoxic; immunogenicity does not preclude single-dose NHV).")
        value = POP_HEALTHY
    return DesignDecision(
        "population", "Population", value, rationale,
        f"Safety profile · immunogenicity: {prof.immunogenicity} · target-mediated drug disposition: {prof.tmdd}",
        list(POPULATION_OPTIONS),
    )


def _dose_decision(prof: DrugProfile, population_value: str, *, multiple: bool = False) -> DesignDecision:
    if multiple:
        value = DOSE_MULTIPLE_SS
        rationale = ("Multiple therapeutic doses are given to steady state and PK is evaluated at steady "
                     "state; the number of doses to steady state is computed from the half-life and "
                     "dosing interval.")
        if prof.tmdd == "yes":
            rationale += (" With target-mediated disposition, a reduced or single dose would sit in the "
                          "non-linear, unsaturated range and misrepresent exposure at the labelled regimen.")
    elif population_value.startswith("Patients") and prof.cytotoxic:
        value = DOSE_REDUCED
        rationale = "Toxic profile — a reduced dose may be used for safety."
    else:
        value = DOSE_SINGLE
        rationale = "Therapeutic single dose gives the cleanest PK comparison; no safety reason to reduce."
    return DesignDecision(
        "dose", "Dose", value, rationale, "Label dose · safety profile · dosing regimen",
        list(DOSE_OPTIONS),
    )


def _route_decision(
    prof: DrugProfile, *,
    label_device: Optional[str] = None,
    label_site: Optional[str] = None,
    label_matched: bool = False,
) -> DesignDecision:
    evidence = ("Live DailyMed label (Dosage and Administration)" if label_matched
                else "Curated knowledge base (no live label match — confirm against the real label)")
    routes = {r.lower() for r in prof.routes}
    if prof.modality == "small_molecule" and (not routes or "oral" in routes):
        return DesignDecision(
            "route", "Route, device, site", "Oral",
            "Oral small-molecule product — no injection route, device, or site applies.",
            evidence, ["Oral"],
        )
    if "intravitreal" in routes:
        return DesignDecision(
            "route", "Route, device, site", "Intravitreal",
            "Local intravitreal product — administered per label; systemic PK sampling as applicable.",
            evidence, ["Intravitreal"],
        )
    has_sc, has_iv = "sc" in routes, "iv" in routes
    if has_sc and has_iv:
        route, why = "SC", "Both IV and SC are on label — SC is chosen because it earns the IV-PK waiver."
    elif has_sc:
        route, why = "SC", "SC is the labelled route."
    elif has_iv:
        route, why = "IV", "IV is the only labelled route."
    else:
        route, why = "SC", "Injectable biologic — SC assumed (confirm against label)."
    if route == "IV":
        value = "IV infusion"
        opts = ["IV infusion", "SC · prefilled syringe · abdomen"]
    else:
        device = label_device or "PFS"
        device_text = "prefilled syringe" if device == "PFS" else "autoinjector"
        site = label_site or "abdomen"
        value = f"SC · {device_text} · {site}"
        if label_device:
            why += f" Label states the {device_text}."
        else:
            why += " Prefilled syringe preferred over autoinjector by default (confirm against label)."
        if label_site:
            why += f" Label specifies the {site} as the injection site."
        else:
            why += f" {site.capitalize()} minimises injection-site reactions by default."
        opts = list(dict.fromkeys([
            value,
            "SC · prefilled syringe · abdomen", "SC · autoinjector · abdomen",
            "SC · prefilled syringe · thigh", "IV infusion",
        ]))
    return DesignDecision("route", "Route, device, site", value, why, evidence, opts)


def _design_decision(design_key: Optional[str], t_half_hours: Optional[float]) -> DesignDecision:
    crossover = design_key in ("crossover", "replicate")
    if crossover:
        value = "Crossover"
        if t_half_hours and t_half_hours < 24:
            rationale = (f"Short half-life (~{t_half_hours:.0f} h) supports a within-subject "
                         "crossover comparison (as used for insulins).")
        else:
            rationale = "A within-subject crossover is feasible and preferred for this product."
    else:
        value = "Parallel"
        if t_half_hours and t_half_hours >= 24:
            rationale = (f"Long half-life (~{t_half_hours/24:.0f} days) / biologic — a parallel-group "
                         "design avoids an impractically long washout and immunogenicity carry-over.")
        else:
            rationale = "Parallel-group design (biologic / long half-life)."
    return DesignDecision(
        "design", "Design", value, rationale,
        "Half-life · sample-size engine", ["Parallel", "Crossover"],
    )


def _pd_decision(
    prof: DrugProfile,
    *,
    multiple_dose_patient_biosimilar: bool = False,
) -> DesignDecision:
    """PD / efficacy endpoint placement.

    Two DIFFERENT designs are handled here and they reach opposite answers, so
    the branch is deliberate rather than incidental:

      * Single-dose (typically healthy-volunteer) study — a clinically
        VALIDATED PD marker that responds inside the PK window (GIR by
        euglycemic clamp for insulins, ANC for filgrastim, reticulocytes for
        epoetin) IS added as a co-primary alongside PK. That is the established
        rule for these products and is unchanged.

      * Multiple-dose PATIENT biosimilar study — the co-primary set is the PK
        pair (first-dose and steady-state AUC(0-tau)) and NOTHING else. An
        efficacy measure, or a validated biomarker standing in for efficacy at
        an early timepoint, may be included but ONLY as a SECONDARY endpoint,
        and the study is never powered on it. Powering a PK-similarity study on
        an efficacy endpoint would size it for a question it is not designed to
        answer: efficacy endpoints are far less sensitive to the exposure
        differences a biosimilar comparison must detect, so they would demand a
        much larger study while providing weaker evidence of similarity. The
        biomarker still earns its place descriptively, as supportive evidence
        that comparable exposure produced comparable pharmacology.
    """
    if multiple_dose_patient_biosimilar:
        if prof.pd_marker:
            name, method = prof.pd_marker["name"], prof.pd_marker["method"]
            value = f"Secondary (not powered): {name} ({method})"
            rationale = (
                f"{name} is a validated PD marker and is included as a SECONDARY endpoint, "
                "measured as supportive evidence that comparable exposure produced comparable "
                "pharmacology. It is NOT co-primary and the study is NOT powered on it — the "
                "co-primary set is the first-dose and steady-state AUC(0-tau) PK pair."
            )
            options = [value, "None (PK only)"]
        else:
            value = "Secondary (not powered): clinical efficacy measure, descriptive only"
            rationale = (
                "No validated PD marker responds within this product's PK window. An "
                "appropriate clinical efficacy measure may be collected as a SECONDARY, "
                "descriptive endpoint, but the study is NOT powered on it: the co-primary set "
                "is the first-dose and steady-state AUC(0-tau) PK pair, and an efficacy "
                "endpoint is far less sensitive to the exposure differences this study must "
                "detect."
            )
            options = [value, "None (PK only)"]
        return DesignDecision(
            "pd_endpoint", "Efficacy / PD endpoint (secondary)", value, rationale,
            "Validated PD-marker knowledge base · multiple-dose patient biosimilar design",
            options,
        )

    if prof.pd_marker:
        name, method = prof.pd_marker["name"], prof.pd_marker["method"]
        value = f"{name} ({method})"
        rationale = ("A clinically validated PD marker that responds within the PK window — "
                     "added as a co-primary alongside PK.")
        options = [value, "None (PK only)"]
    else:
        value = "None (PK only)"
        rationale = ("No clinically validated PD marker responds within this product's PK window, "
                     "so PK endpoints are the sole primary (chronic efficacy endpoints such as "
                     "HbA1c cannot move in a single-dose window and are not used here).")
        options = ["None (PK only)"]
    return DesignDecision("pd_endpoint", "PD co-primary endpoint", value, rationale,
                          "Validated PD-marker knowledge base", options)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def determine_design_decisions(
    *,
    drug_inn: str,
    study_type: str,
    design_key: Optional[str] = None,
    t_half_hours: Optional[float] = None,
    label_routes: Optional[list[str]] = None,
    label_text: Optional[str] = None,
    dosing: str = "single",
    subject_type: str = "healthy",
    population_choice: Optional[str] = None,
) -> list[DesignDecision]:
    """Apply the five rules and return the ordered, overridable decision set.

    ``subject_type`` / ``dosing`` — what the wizard's Population step and any
    earlier decision already established. A ``patient`` subject_type on an
    NHV-safe product yields "Patients" (or "Patients · multi-dose" when the
    product has documented TMDD).
    ``population_choice`` — the reviewer's explicit Population override on the
    Review design step. When it differs from the engine's call it replaces the
    population decision, and the dose / PD decisions are re-derived from it so a
    switch to or from multi-dose keeps Dose and the PD placement consistent.

    ``label_text`` — the drug's real DailyMed "Dosage and Administration" /
    "How Supplied" text, when available (see literature_service.py — free NIH
    API, no AI cost). When present it grounds the route/device/site decision in
    the actual label instead of the curated knowledge base's generic guess, and
    that decision's `evidence` field reports which source was used.
    ``label_routes`` — pass explicit routes directly if a caller already has
    them structured; takes precedence over routes parsed from ``label_text``.
    """
    extracted_routes = extract_label_routes(label_text) if label_text else ()
    routes = tuple(label_routes) if label_routes else extracted_routes
    prof = _profile_for(drug_inn, study_type, routes)
    population = _population_decision(prof, subject_type)
    choice_applied = False
    if population_choice in POPULATION_OPTIONS and population_choice != population.value:
        population = DesignDecision(
            "population", "Population", population_choice,
            f"Reviewer-selected. Engine recommendation was '{population.value}': {population.rationale}",
            population.evidence, list(POPULATION_OPTIONS),
        )
        choice_applied = True
    label_device = extract_label_device(label_text) if label_text else None
    label_site = extract_label_site(label_text) if label_text else None
    label_matched = bool(extracted_routes or label_device or label_site)
    # The efficacy/PD placement rule differs for a multiple-dose patient
    # biosimilar study (secondary only, never powered) from a single-dose study
    # (a validated in-window PD marker may be co-primary) — see _pd_decision.
    # The population DECISION is what settles whether this is a patient study,
    # so it is read from that rather than from the incoming subject_type, which
    # may not yet reflect the engine's own call.
    if choice_applied:
        # The reviewer's explicit choice is authoritative over incoming state.
        is_patient = population.value.startswith("Patients")
        multiple = population.value == POP_PATIENTS_MULTI
    else:
        is_patient = (
            population.value.startswith("Patients")
            or str(subject_type or "healthy").strip().lower() == "patient"
        )
        multiple = (
            str(dosing or "single").strip().lower() in ("multiple", "multi", "repeat")
            or population.value == POP_PATIENTS_MULTI
        )
    md_patient_biosimilar = bool(
        is_patient and multiple and str(study_type or "").strip().lower() == "biosimilar_pk"
    )
    return [
        population,
        _dose_decision(prof, population.value, multiple=multiple),
        _route_decision(prof, label_device=label_device, label_site=label_site, label_matched=label_matched),
        _design_decision(design_key, t_half_hours),
        _pd_decision(prof, multiple_dose_patient_biosimilar=md_patient_biosimilar),
    ]


def decisions_to_dicts(decisions: list[DesignDecision]) -> list[dict[str, Any]]:
    return [d.to_dict() for d in decisions]


def design_directive(decisions: list[dict[str, Any]]) -> str:
    """Format the (possibly reviewer-overridden) decisions as a locked directive
    injected into the generation prompt — the same mechanism used for sample size
    and guideline specifics, so the protocol text can't contradict or re-derive
    the design calls."""
    if not decisions:
        return ""
    lines = [
        "\nLOCKED STUDY-DESIGN DECISIONS — the protocol MUST reflect these EXACT",
        "choices consistently across every relevant section (population/eligibility,",
        "dosing, study design, endpoints, statistical analysis); do NOT contradict,",
        "soften, or re-derive them:",
    ]
    for d in decisions:
        rationale = d.get("rationale", "")
        lines.append(f"- {d.get('label', d.get('key', ''))}: {d.get('value', '')} — {rationale}")
    return "\n".join(lines) + "\n"


def is_multi_dose_decision(population_value: Optional[str], dose_value: Optional[str]) -> bool:
    """True when the Population or Dose decision selects a multiple-dose /
    steady-state regimen. One predicate so every consumer (override folding,
    engine defaults, reconciliation) reads the two decisions the same way."""
    pop = population_value or ""
    dose = dose_value or ""
    return (
        pop == POP_PATIENTS_MULTI
        or "multi-dose" in pop.lower()
        or dose == DOSE_MULTIPLE_SS
        or "steady state" in dose.lower()
    )


def apply_decision_overrides(population_config: dict[str, Any], decisions: list[dict[str, Any]]) -> None:
    """Fold the reviewer's population/dose/design choices back into population_config
    IN PLACE so the sample-size engine (design) and the population section
    (subject type) stay consistent with what was chosen on the review screen.

    The reviewer's Population / Dose choice must also reach the sample-size and
    sampling-schedule engines, which read population_config['dosing']. That key
    was previously never set, so a multi-dose decision produced a multi-dose
    label on a study still sized and sampled as single-dose. With neither a
    population nor a dose decision in the list (e.g. pd_endpoint only), 'dosing'
    is left untouched."""
    pop: Optional[str] = None
    dose: Optional[str] = None
    for d in decisions:
        key, value = d.get("key"), (d.get("value") or "")
        if key == "design":
            if "crossover" in value.lower():
                population_config["design"] = "crossover"
            elif "parallel" in value.lower():
                population_config["design"] = "parallel"
        elif key == "population":
            pop = value
            population_config["subject_type"] = "patient" if value.startswith("Patients") else "healthy"
        elif key == "dose":
            dose = value
    if pop is not None or dose is not None:
        population_config["dosing"] = "multiple" if is_multi_dose_decision(pop, dose) else "single"


def apply_engine_population_defaults(population_config: dict[str, Any], decisions: list[dict[str, Any]]) -> None:
    """Upgrade-only counterpart of apply_decision_overrides, for when the client
    supplied no reviewer decisions: adopt the ENGINE's own safety/TMDD call so
    the downstream engines see a patient, multiple-dose study. It never
    downgrades an explicit patient subject_type to healthy and never overwrites
    an explicit 'dosing'."""
    pop: Optional[str] = None
    dose: Optional[str] = None
    for d in decisions:
        if d.get("key") == "population":
            pop = d.get("value") or ""
        elif d.get("key") == "dose":
            dose = d.get("value") or ""
    if (pop or "").startswith("Patients") and population_config.get("subject_type") != "patient":
        population_config["subject_type"] = "patient"
    if is_multi_dose_decision(pop, dose) and not population_config.get("dosing"):
        population_config["dosing"] = "multiple"


def reconcile_decision_set(
    decisions: list[dict[str, Any]], *, drug_inn: str, study_type: str,
) -> list[dict[str, Any]]:
    """Return a coherent deep copy of a reviewer-edited decision set.

    Population, Dose and the PD placement interact: picking multi-dose in one
    dropdown must not leave the others contradicting it (a multi-dose population
    with a single-dose Dose, or a co-primary PD marker in a multi-dose patient
    study where it may only be secondary). The input is never mutated."""
    out = copy.deepcopy(decisions or [])
    by_key = {d.get("key"): i for i, d in enumerate(out)}
    pop_i, dose_i, pd_i = by_key.get("population"), by_key.get("dose"), by_key.get("pd_endpoint")
    if pop_i is None and dose_i is None:
        return out
    pop = out[pop_i] if pop_i is not None else None
    dose = out[dose_i] if dose_i is not None else None
    multiple = is_multi_dose_decision(pop.get("value") if pop else None, dose.get("value") if dose else None)
    if multiple and dose is not None and dose.get("value") == DOSE_SINGLE:
        dose["value"] = DOSE_MULTIPLE_SS
        dose["rationale"] = (dose.get("rationale") or "") + " (Aligned with the multi-dose population decision.)"
    if multiple and pop is not None and pop.get("value") == POP_PATIENTS:
        pop["value"] = POP_PATIENTS_MULTI
        pop["rationale"] = (pop.get("rationale") or "") + " (Aligned with the multiple-dose regimen chosen for Dose.)"
    md = bool(
        multiple
        and ((pop.get("value") if pop else None) or "").startswith("Patients")
        and study_type == "biosimilar_pk"
    )
    if pd_i is not None:
        pd_value = out[pd_i].get("value") or ""
        prof = _profile_for(drug_inn, study_type, ())
        if md and not (pd_value.startswith("Secondary (not powered)") or pd_value.startswith("None")):
            out[pd_i] = _pd_decision(prof, multiple_dose_patient_biosimilar=True).to_dict()
        elif not md and pd_value.startswith("Secondary (not powered)"):
            out[pd_i] = _pd_decision(prof, multiple_dose_patient_biosimilar=False).to_dict()
    return out
