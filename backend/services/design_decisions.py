"""Study-design decision engine — the five non-negotiable clinical rules that
shape a PK-similarity / bioequivalence study, surfaced (with rationale) to the
"Review design" wizard step where a pharmacologist can override any of them.

The five decisions, in order:

  1. Population — NHV vs patients, gated on safety. Highly immunogenic or
     cytotoxic products are unsafe in healthy volunteers → patients (and
     multi-dose PK for immunogenic products). Some oncology mAbs are safe at a
     single dose in NHV (e.g. trastuzumab) → NHV allowed.
  2. Dose — therapeutic (label) single dose by default; a reduced dose only
     where safety warrants.
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


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

# modality: "small_molecule" (oral BE) | "mab" | "insulin" | "protein" | "other"
# immunogenicity: "low" | "moderate" | "high"
# nhv_single_dose_safe: is a single dose safe in healthy volunteers?
# cytotoxic: classical cytotoxic / directly toxic safety profile
# pd_marker: {"name", "method"} if a validated PK-window PD marker exists, else None
# routes: label routes we know ("SC"/"IV"/"oral"), used for the route rule
@dataclass(frozen=True)
class DrugProfile:
    modality: str = "small_molecule"
    immunogenicity: str = "low"
    nhv_single_dose_safe: bool = True
    cytotoxic: bool = False
    pd_marker: Optional[dict[str, str]] = None
    routes: tuple[str, ...] = ()


_INSULIN_PD = {"name": "Glucose infusion rate (GIR)", "method": "euglycemic clamp"}
_ANC_PD = {"name": "Absolute neutrophil count (ANC)", "method": "serial CBC"}
_RETIC_PD = {"name": "Reticulocyte count", "method": "serial CBC"}

# Curated by INN (lowercase). Values reflect the standard regulatory approach for
# each product's biosimilar/BE PK study — the reviewer can override any of them.
_KB: dict[str, DrugProfile] = {
    # Insulins → NHV, SC, crossover, GIR co-primary
    "insulin glargine": DrugProfile("insulin", "low", True, False, _INSULIN_PD, ("SC",)),
    "insulin aspart":   DrugProfile("insulin", "low", True, False, _INSULIN_PD, ("SC",)),
    "insulin lispro":   DrugProfile("insulin", "low", True, False, _INSULIN_PD, ("SC",)),
    "insulin detemir":  DrugProfile("insulin", "low", True, False, _INSULIN_PD, ("SC",)),
    # Myeloid growth factors → NHV, SC, PD = ANC
    "filgrastim":    DrugProfile("protein", "moderate", True, False, _ANC_PD, ("SC", "IV")),
    "pegfilgrastim": DrugProfile("protein", "moderate", True, False, _ANC_PD, ("SC",)),
    "lenograstim":   DrugProfile("protein", "moderate", True, False, _ANC_PD, ("SC", "IV")),
    # Erythropoiesis-stimulating agents → NHV, PD = reticulocytes
    "epoetin alfa": DrugProfile("protein", "moderate", True, False, _RETIC_PD, ("SC", "IV")),
    "darbepoetin alfa": DrugProfile("protein", "moderate", True, False, _RETIC_PD, ("SC", "IV")),
    # Targeted mAbs safe single-dose in NHV → NHV, parallel, no PD marker
    "trastuzumab": DrugProfile("mab", "moderate", True, False, None, ("IV", "SC")),
    "bevacizumab": DrugProfile("mab", "moderate", True, False, None, ("IV",)),
    "adalimumab":  DrugProfile("mab", "high", True, False, None, ("SC",)),
    "etanercept":  DrugProfile("protein", "moderate", True, False, None, ("SC",)),
    "infliximab":  DrugProfile("mab", "high", True, False, None, ("IV",)),
    "ranibizumab": DrugProfile("mab", "moderate", True, False, None, ("intravitreal",)),
    "omalizumab":  DrugProfile("mab", "moderate", True, False, None, ("SC",)),
    # B-cell depleting / immune-modulating mAbs → NOT NHV-safe → patients
    "rituximab":    DrugProfile("mab", "high", False, False, None, ("IV",)),
    "obinutuzumab": DrugProfile("mab", "high", False, False, None, ("IV",)),
    # Checkpoint inhibitors / oncology immunotherapy → patients (immune toxicity)
    "pembrolizumab": DrugProfile("mab", "high", False, True, None, ("IV",)),
    "nivolumab":     DrugProfile("mab", "high", False, True, None, ("IV",)),
    "cetuximab":     DrugProfile("mab", "high", False, True, None, ("IV",)),
    # Classical cytotoxics → patients
    "doxorubicin": DrugProfile("small_molecule", "low", False, True, None, ("IV",)),
    "paclitaxel":  DrugProfile("small_molecule", "low", False, True, None, ("IV",)),
    # Oral small molecules → NHV, oral, crossover, no PD
    "ibuprofen":    DrugProfile("small_molecule", "low", True, False, None, ("oral",)),
    "metformin":    DrugProfile("small_molecule", "low", True, False, None, ("oral",)),
    "atorvastatin": DrugProfile("small_molecule", "low", True, False, None, ("oral",)),
    "warfarin":     DrugProfile("small_molecule", "low", True, False, None, ("oral",)),
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
                           prof.cytotoxic, prof.pd_marker, label_routes or prof.routes)
    # Unknown drug: infer from study type. Biosimilars are biologics (default mAb,
    # NHV-safe single dose, no known PD marker); everything else is treated as an
    # oral small molecule. The reviewer confirms/overrides.
    if study_type == "biosimilar_pk":
        return DrugProfile("mab", "moderate", True, False, None, label_routes or ("SC", "IV"))
    return DrugProfile("small_molecule", "low", True, False, None, label_routes or ("oral",))


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


def _population_decision(prof: DrugProfile) -> DesignDecision:
    multi = " · multi-dose" if prof.immunogenicity == "high" and not prof.nhv_single_dose_safe else ""
    if prof.cytotoxic and not prof.nhv_single_dose_safe:
        value, rationale = (f"Patients{multi}",
                            "Cytotoxic / toxic safety profile — not safe in healthy volunteers.")
    elif prof.immunogenicity == "high" and not prof.nhv_single_dose_safe:
        value, rationale = ("Patients · multi-dose",
                            "Highly immunogenic — repeat-dose PK must be characterised in patients.")
    elif not prof.nhv_single_dose_safe:
        value, rationale = ("Patients",
                            "Not safe for single-dose administration in healthy volunteers.")
    else:
        rationale = ("A single dose is safe in healthy volunteers "
                     "(not cytotoxic; immunogenicity does not preclude single-dose NHV).")
        value = "Healthy volunteers"
    return DesignDecision(
        "population", "Population", value, rationale,
        f"Safety profile · immunogenicity: {prof.immunogenicity}",
        ["Healthy volunteers", "Patients", "Patients · multi-dose"],
    )


def _dose_decision(prof: DrugProfile, population_value: str) -> DesignDecision:
    if population_value.startswith("Patients") and prof.cytotoxic:
        value = "Reduced dose"
        rationale = "Toxic profile — a reduced dose may be used for safety."
    else:
        value = "Therapeutic (label) dose — single dose"
        rationale = "Therapeutic single dose gives the cleanest PK comparison; no safety reason to reduce."
    return DesignDecision(
        "dose", "Dose", value, rationale, "Label dose · safety profile",
        ["Therapeutic (label) dose — single dose", "Reduced dose"],
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


def _pd_decision(prof: DrugProfile) -> DesignDecision:
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
) -> list[DesignDecision]:
    """Apply the five rules and return the ordered, overridable decision set.

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
    population = _population_decision(prof)
    label_device = extract_label_device(label_text) if label_text else None
    label_site = extract_label_site(label_text) if label_text else None
    label_matched = bool(extracted_routes or label_device or label_site)
    return [
        population,
        _dose_decision(prof, population.value),
        _route_decision(prof, label_device=label_device, label_site=label_site, label_matched=label_matched),
        _design_decision(design_key, t_half_hours),
        _pd_decision(prof),
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


def apply_decision_overrides(population_config: dict[str, Any], decisions: list[dict[str, Any]]) -> None:
    """Fold the reviewer's population/design choices back into population_config
    IN PLACE so the sample-size engine (design) and the population section
    (subject type) stay consistent with what was chosen on the review screen."""
    for d in decisions:
        key, value = d.get("key"), (d.get("value") or "")
        if key == "design":
            if "crossover" in value.lower():
                population_config["design"] = "crossover"
            elif "parallel" in value.lower():
                population_config["design"] = "parallel"
        elif key == "population":
            population_config["subject_type"] = "patient" if value.startswith("Patients") else "healthy"
