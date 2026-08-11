"""Inclusion/exclusion-criteria precedent from prior trials in the same
indication (services/literature_service.py's ClinicalTrials.gov retrieval).

WHY THIS EXISTS
---------------
A clinical pharmacologist's explicit requirement: "inclusion and exclusion
criteria should be based on the prior trials done in that indication,
reference would be the innovator trial protocols and others in
clinicaltrials.gov." Before this module, services/ai_service.py's population
section instructions were a single static, hardcoded template describing
generic small-molecule/healthy-volunteer bioequivalence-study boilerplate
(age 18-55, BMI bounds, normal labs, non-smoker, ...) - reused verbatim for
every study, including a patient biosimilar trial in a specific licensed
indication, with no connection to what real prior trials in that indication
actually required of their subjects.

This module does the opposite. It takes the RAW trials already retrieved for
this drug (services/literature_service.py's fetch_clinicaltrials - searched
by intervention name only, cached, NOT filtered by indication) and the
indication ALREADY SELECTED by services/patient_population.py, and:

  1. Filters to trials whose registered condition(s) actually match the
     selected indication. The drug-level cache is not indication-specific
     (one drug can have trials across many indications), so this filtering
     has to happen HERE, on every call, against the caller's actual
     indication - the same "cache the raw retrieval, filter downstream"
     split services/meta_analysis.py already uses for population-specific
     filtering of cached PubMed results.
  2. Ranks the survivors by how strong a precedent they are (a COMPLETED
     trial with real reported eligibility text is the strongest evidence a
     design actually enrolled; a terminated/withdrawn one is weaker).
  3. Renders the top few as citable LOCKED reference material - their real,
     as-registered inclusion/exclusion text, NCT-attributed - so the
     generator can draw specific criteria from genuine precedent (citing the
     NCT number) instead of inventing generic boilerplate.

WHAT THIS DELIBERATELY DOES NOT DO
-----------------------------------
It does not attempt to parse each trial's free-text eligibility criteria
into individual structured bullet items. ClinicalTrials.gov eligibility text
is free text, and no two trials format it identically; a regex that "looks
right" on a handful of examples would silently mis-split or drop content on
the next one, which is a worse failure than not parsing at all. Instead the
raw, as-registered text (length-bounded) is handed to the generator as
citable reference material - following the same "ground with real retrieved
data, let the model narrate from it" pattern this codebase already uses for
regulatory-assessment-report citations (services/literature_service.py's
plan_regulatory_sources / regulatory_sources_directive).

Follows the deterministic-engine pattern: the filtering/ranking/bounding is
computed once in Python; the model narrates the result but does not
re-derive or invent it.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

# Bounds the prompt: real eligibility-criteria blocks run 500-3000+ chars;
# with up to MAX_PRECEDENT_TRIALS trials each length-capped, this keeps the
# directive's own size predictable regardless of how verbose any one trial's
# text is.
MAX_PRECEDENT_TRIALS = 5
MAX_CRITERIA_CHARS = 1500

# Precedent quality by registration status, best first. A trial that actually
# COMPLETED and reported final eligibility criteria is the strongest
# real-world precedent that a design was workable; a still-recruiting,
# terminated or withdrawn trial's criteria are still REAL (they were
# registered and used to screen subjects) but weaker evidence of a proven
# design, so they rank behind - not excluded outright, since even a
# terminated trial's eligibility criteria were genuinely used.
_STATUS_RANK = {
    "COMPLETED": 0,
    "ACTIVE_NOT_RECRUITING": 1,
    "RECRUITING": 2,
    "ENROLLING_BY_INVITATION": 2,
    "NOT_YET_RECRUITING": 2,
    "TERMINATED": 3,
    "SUSPENDED": 3,
    "WITHDRAWN": 4,
}
_DEFAULT_STATUS_RANK = 3


@dataclass
class TrialPrecedent:
    nct_id: str
    title: str
    status: str
    phase: str
    sex: str
    minimum_age: str
    maximum_age: str
    healthy_volunteers: Optional[bool]
    inclusion_text: str
    exclusion_text: str
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _condition_matches(indication: str, conditions: list[str]) -> bool:
    """True when a trial's registered condition(s) are a real match for the
    selected indication.

    Free-text disease names never match exactly across sources: a registry
    condition is often a short core name ("Psoriasis") while the selected
    indication is a longer, qualified description ("Moderate-to-severe
    chronic plaque psoriasis"). This checks BOTH substring directions rather
    than exact equality, which catches the common case where one name is a
    core-phrase subset of the other, in either direction. This is NOT general
    NLP matching - a genuinely different-worded name for the same disease
    with no shared substring (an abbreviation, a synonym) will not match;
    that is an accepted limitation given how widely real ClinicalTrials.gov
    condition strings vary in phrasing, same class of heuristic as
    services/patient_population.py's negation handling.
    """
    ind = _normalize(indication)
    if not ind:
        return False
    for cond in conditions:
        c = _normalize(cond)
        if c and (c in ind or ind in c):
            return True
    return False


def _split_eligibility_text(raw: str) -> tuple[str, str]:
    """Split ClinicalTrials.gov's free-text eligibilityCriteria into
    (inclusion_text, exclusion_text) on the literal "Exclusion Criteria"
    header ClinicalTrials.gov's own submission form requires. A trial that
    omits the header (rare, but real for some legacy records) has its whole
    text returned as inclusion_text with an empty exclusion_text - an honest
    "could not split" outcome rather than a guessed boundary."""
    if not raw:
        return "", ""
    parts = re.split(r"exclusion criteria:?", raw, maxsplit=1, flags=re.I)
    inclusion, exclusion = parts if len(parts) == 2 else (raw, "")
    # Drop a leading "Inclusion Criteria:" header — implied by which half of
    # the split it is, so stating it again would be redundant.
    inclusion = re.sub(r"^\s*inclusion criteria:?", "", inclusion, flags=re.I).strip()
    return inclusion.strip(), exclusion.strip()


def _bounded(text: str, limit: int = MAX_CRITERIA_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + " …", True


def select_trial_precedents(
    *,
    drug_inn: str,
    patient_population: Optional[dict[str, Any]],
    trials: list[dict[str, Any]],
) -> dict[str, Any]:
    """Filter/rank retrieved trials into the top precedent set for the
    ALREADY-SELECTED indication (``patient_population["recommended"]``).

    Returns ``{"applicable": False, ...}`` for any study this does not
    govern (a healthy-volunteer study, a non-biosimilar study type, or a
    patient biosimilar study where no indication could be selected at all) -
    the caller can therefore invoke this unconditionally alongside the other
    engines, mirroring services/patient_population.py's own contract.

    When the study DOES have a selected indication but no relevant
    ClinicalTrials.gov precedent could be found (``precedent_found: False``),
    ``applicable`` stays True: a patient biosimilar study with a selected
    indication always has a grounding QUESTION to answer, and "no real
    precedent was found" is a fact the generator must state explicitly
    rather than silently falling back to unattributed boilerplate.
    """
    if not patient_population or not patient_population.get("applicable"):
        return {
            "applicable": False,
            "reason": (
                "Trial precedent applies to patient biosimilar PK-similarity "
                "studies only, where a specific indication has been selected."
            ),
        }
    recommended = patient_population.get("recommended")
    indication = (recommended or {}).get("indication")
    if not indication:
        return {
            "applicable": False,
            "reason": "No indication was selected to find trial precedent for.",
        }

    n_retrieved = len(trials)
    relevant = [t for t in trials if _condition_matches(indication, t.get("conditions") or [])]
    assumptions: list[str] = []

    if not relevant:
        reason = (
            f"No ClinicalTrials.gov records were retrieved for {drug_inn}."
            if n_retrieved == 0 else
            f"{n_retrieved} ClinicalTrials.gov record(s) were retrieved for {drug_inn}, but "
            f"none is registered under a condition matching '{indication}'."
        )
        return {
            "applicable": True,
            "precedent_found": False,
            "drug_inn": drug_inn,
            "indication": indication,
            "n_trials_retrieved": n_retrieved,
            "n_trials_relevant": 0,
            "precedent_trials": [],
            "reason": reason,
            "assumptions": [
                reason + " Inclusion/exclusion criteria could not be grounded in "
                "prior-trial precedent for this indication and must be justified "
                "manually against the innovator's own trial protocol(s)."
            ],
            "summary": reason,
        }

    def _rank(t: dict[str, Any]) -> tuple[int, int]:
        status_rank = _STATUS_RANK.get(t.get("status", ""), _DEFAULT_STATUS_RANK)
        has_no_text = 0 if (t.get("eligibility_criteria") or "").strip() else 1
        return (status_rank, has_no_text)

    ranked = sorted(relevant, key=_rank)
    chosen = ranked[:MAX_PRECEDENT_TRIALS]
    if len(ranked) > MAX_PRECEDENT_TRIALS:
        assumptions.append(
            f"{len(ranked)} relevant trials were retrieved; only the "
            f"{MAX_PRECEDENT_TRIALS} strongest precedents (by registration status) "
            "are shown below."
        )

    precedents: list[TrialPrecedent] = []
    for t in chosen:
        inc_raw, exc_raw = _split_eligibility_text(t.get("eligibility_criteria") or "")
        inc, inc_trunc = _bounded(inc_raw)
        exc, exc_trunc = _bounded(exc_raw)
        precedents.append(TrialPrecedent(
            nct_id=t.get("nct_id", ""),
            title=t.get("title", ""),
            status=t.get("status", ""),
            phase=t.get("phase", ""),
            sex=t.get("sex", ""),
            minimum_age=t.get("minimum_age", ""),
            maximum_age=t.get("maximum_age", ""),
            healthy_volunteers=t.get("healthy_volunteers"),
            inclusion_text=inc,
            exclusion_text=exc,
            truncated=inc_trunc or exc_trunc,
        ))

    if not any(p.inclusion_text or p.exclusion_text for p in precedents):
        assumptions.append(
            "The retrieved trial record(s) for this indication did not carry a "
            "parseable eligibility-criteria text block; only their structured "
            "fields (status/phase/age/sex/healthy-volunteer) could be used as "
            "precedent."
        )

    return {
        "applicable": True,
        "precedent_found": True,
        "drug_inn": drug_inn,
        "indication": indication,
        "n_trials_retrieved": n_retrieved,
        "n_trials_relevant": len(relevant),
        "precedent_trials": [p.to_dict() for p in precedents],
        "assumptions": assumptions,
        "summary": (
            f"Inclusion/exclusion criteria are grounded in {len(precedents)} real "
            f"prior trial(s) registered on ClinicalTrials.gov for {indication}: "
            + ", ".join(p.nct_id for p in precedents) + "."
        ),
    }


def trial_precedent_directive(result: Optional[dict[str, Any]]) -> str:
    """Render the selected precedent trials as LOCKED reference material for
    the population section's Inclusion/Exclusion Criteria subsection."""
    if not result or not result.get("applicable"):
        return ""

    if not result.get("precedent_found"):
        lines = [
            "\nINCLUSION/EXCLUSION CRITERIA PRECEDENT - NOT FOUND. No real prior trial "
            f"registered on ClinicalTrials.gov could be matched to {result.get('indication')} "
            "for this drug. State plainly that inclusion/exclusion criteria could not be "
            "grounded in retrieved trial precedent and must be justified manually against "
            "the innovator's own trial protocol(s); do NOT imply this document reviewed a "
            "real prior trial's criteria.",
        ]
        for note in result.get("assumptions", []):
            lines.append(f"- ASSUMPTION (state it explicitly): {note}")
        return "\n".join(lines) + "\n"

    lines = [
        "\nINCLUSION/EXCLUSION CRITERIA PRECEDENT - REAL PRIOR TRIALS. Base the",
        "Inclusion Criteria and Exclusion Criteria lists on the REAL prior trials below",
        f"(retrieved from ClinicalTrials.gov for {result['indication']}), NOT on generic",
        "single-dose bioequivalence-study boilerplate (age/BMI bounds, non-smoker status,",
        "etc. designed for healthy volunteers). Adapt them to THIS study's own design",
        "(dosing, endpoints, comparator) rather than copying them verbatim, and cite the",
        "NCT number when a criterion is drawn from a specific trial (e.g. 'as in",
        "NCT02198651'). Do NOT claim to have read the full protocol of any trial below -",
        "only its ClinicalTrials.gov registration record (title, status, phase, structured",
        "eligibility fields, and the as-registered eligibility text) was retrieved.",
    ]
    for p in result.get("precedent_trials", []):
        header = f"\n- {p['nct_id']} - {p['title']} ({p['status']}"
        if p.get("phase"):
            header += f", {p['phase']}"
        header += "):"
        lines.append(header)

        fields = []
        if p.get("minimum_age"):
            fields.append(f"min age {p['minimum_age']}")
        if p.get("maximum_age"):
            fields.append(f"max age {p['maximum_age']}")
        if p.get("sex") and p["sex"] != "ALL":
            fields.append(f"sex: {p['sex']}")
        if p.get("healthy_volunteers") is not None:
            fields.append(f"healthy volunteers accepted: {p['healthy_volunteers']}")
        if fields:
            lines.append(f"    ({'; '.join(fields)})")

        if p.get("inclusion_text"):
            lines.append(f"    Inclusion Criteria (as registered): {p['inclusion_text']}")
        if p.get("exclusion_text"):
            lines.append(f"    Exclusion Criteria (as registered): {p['exclusion_text']}")
        if not p.get("inclusion_text") and not p.get("exclusion_text"):
            lines.append(
                "    (No eligibility-criteria text was available for this record; "
                "use only the structured fields above.)"
            )

    for note in result.get("assumptions", []):
        lines.append(f"- ASSUMPTION (state it explicitly): {note}")

    return "\n".join(lines) + "\n"
