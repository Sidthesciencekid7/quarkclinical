"""Population-matched meta-analysis of the published PK literature.

WHY THIS EXISTS
---------------
The within-subject CV drives the sample size, and the same drug has a very
different CV depending on WHO it was measured in. Healthy volunteers are dosed
fasted, drug-free, with normal organ function, on a controlled ward, so their
exposure is tight. Patients are older, comorbid, co-medicated, and eat normally,
so theirs is much wider -- routinely 1.5-2x the healthy-volunteer CV. Because
the required N scales with the SQUARE of CV, powering a healthy-volunteer study
on a patient CV can more than double the enrolment for noise that study will
never experience; powering a patient study on a healthy-volunteer CV
under-powers it and it fails.

The rule this module enforces is therefore: THE META-ANALYSIS POPULATION MUST
MATCH THE POPULATION THE PROTOCOL ENROLS. A healthy-volunteer protocol is
powered on healthy-volunteer studies; a patient protocol on patient studies.

THE DEFECT THIS REPLACES
------------------------
``routers/protocols.py::_select_cv_for_powering`` collected every CV in the
retrieved literature and took ``max()`` across all of them, with NO population
filter -- it recorded a ``population`` string on each row and then ignored it.
A single patient study in the search results silently set the powering CV for a
healthy-volunteer trial. That is corrected here, and the correction is
structural: the study table the appendix prints and the CV the study is powered
on are produced by the SAME call, so they cannot disagree.

Follows the deterministic-engine pattern of services/stratification.py: compute
in Python, hand the model locked facts, never let it derive the number.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Population classification
#
# The `population` field on an extracted PK parameter is free text written by
# the extraction model ('healthy adults', 'patients with type 2 diabetes',
# 'subjects with renal impairment'). It has to be classified before it can be
# filtered on. Order matters: PATIENT cues are tested first, because a string
# like "healthy volunteers and patients with hepatic impairment" describes a
# study that is not purely healthy-volunteer and must not be counted as one.
# ---------------------------------------------------------------------------

_PATIENT_CUES: tuple[re.Pattern, ...] = tuple(re.compile(p, re.I) for p in (
    r"\bpatients?\b",
    r"\bsubjects? with\b",
    r"\bimpairment\b",
    r"\bimpaired\b",
    r"\bdiabet",
    r"\bcancer\b|\boncolog|\btumou?r\b|\bcarcinom|\bneoplas",
    r"\bhypertens|\bheart failure\b|\bcardiac\b",
    r"\bcirrhosis\b|\bhepatic disease\b|\bhepatitis\b",
    r"\brenal (?:disease|failure|insufficiency)\b|\bdialysis\b|\bCKD\b",
    r"\barthritis\b|\bpsoriasis\b|\bcolitis\b|\bCrohn",
    r"\bepilep|\bseizure",
    r"\basthma\b|\bCOPD\b",
    r"\bHIV\b|\binfect",
    r"\bschizophren|\bdepress|\bbipolar",
    r"\btransplant",
))

_HEALTHY_CUES: tuple[re.Pattern, ...] = tuple(re.compile(p, re.I) for p in (
    r"\bhealthy\b",
    r"\bnormal (?:volunteers?|subjects?|adults?)\b",
    r"\bHV\b",
    r"\bnormals?\b",
))

# Sub-groups that are technically "healthy" but are NOT the general healthy
# adult population a BE study enrols. Their variability is not representative,
# so they are classified separately and excluded from powering rather than
# quietly folded into the healthy pool.
_SPECIAL_CUES: tuple[re.Pattern, ...] = tuple(re.compile(p, re.I) for p in (
    r"\belderly\b|\bgeriatric\b|\baged (?:6[5-9]|[7-9]\d)\b",
    r"\bp(?:a)?ediatric\b|\bchildren\b|\binfants?\b|\bneonat",
    r"\bpregnan|\blactating\b",
    r"\bpoor metaboli[sz]ers?\b|\bultrarapid metaboli[sz]ers?\b",
))

POPULATION_HEALTHY = "healthy"
POPULATION_PATIENT = "patient"
POPULATION_SPECIAL = "special"
POPULATION_UNKNOWN = "unknown"


def classify_population(text: Optional[str]) -> str:
    """Classify a free-text population string into a comparable bucket.

    Returns one of ``healthy`` / ``patient`` / ``special`` / ``unknown``.
    ``unknown`` is deliberately its own bucket rather than being defaulted to
    healthy: a study whose population cannot be established must not silently
    contribute to a powering decision, and the protocol should say so.
    """
    if not text or not str(text).strip():
        return POPULATION_UNKNOWN
    s = str(text)
    # Special sub-groups first -- 'healthy elderly subjects' is special, not a
    # representative healthy-adult cohort.
    for pat in _SPECIAL_CUES:
        if pat.search(s):
            return POPULATION_SPECIAL
    for pat in _PATIENT_CUES:
        if pat.search(s):
            return POPULATION_PATIENT
    for pat in _HEALTHY_CUES:
        if pat.search(s):
            return POPULATION_HEALTHY
    return POPULATION_UNKNOWN


def target_population(subject_type: Optional[str]) -> str:
    """Map the protocol's ``subject_type`` config onto a population bucket.

    Mirrors routers/protocols.py, which treats anything that is not literally
    "patient" as a healthy-volunteer study.
    """
    return (
        POPULATION_PATIENT
        if str(subject_type or "healthy").strip().lower() == "patient"
        else POPULATION_HEALTHY
    )


# ---------------------------------------------------------------------------
# Parameter grouping
# ---------------------------------------------------------------------------

_AUC_PARAMS = ("AUC0inf", "AUC0t")          # preference order: AUC0inf first
_CMAX_PARAMS = ("Cmax",)
_CV_CMAX_PARAMS = ("cv_intra_cmax",)
_CV_AUC_PARAMS = ("cv_intra_auc",)
_CV_GENERIC_PARAMS = ("cv_intra", "cv_within")
_ALL_CV_PARAMS = _CV_CMAX_PARAMS + _CV_AUC_PARAMS + _CV_GENERIC_PARAMS

# A CV above this is implausible for a single PK metric and almost always means
# the extraction picked up a between-subject CV, a percentage that was actually
# a ratio, or a typo. Powering on it would silently inflate N enormously, so it
# is dropped from powering and reported as excluded rather than used.
_MAX_PLAUSIBLE_CV_PCT = 150.0


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _first_of(rows: list[dict], params: Iterable[str]) -> Optional[dict]:
    """Return the first row matching any of ``params``, in the given order."""
    for want in params:
        for r in rows:
            if r.get("parameter") == want:
                return r
    return None


def _fmt_value(row: Optional[dict]) -> Optional[str]:
    if not row:
        return None
    v = _num(row.get("value"))
    if v is None:
        return None
    unit = str(row.get("unit") or "").strip()
    text = f"{v:g}"
    return f"{text} {unit}".strip()


def build_meta_analysis(
    extracted: list[dict[str, Any]],
    subject_type: Optional[str] = "healthy",
    literature: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build the per-study meta-analysis table and pick the powering CV.

    ``extracted`` is the flat list of PK parameters (one row per parameter, each
    carrying ``source_pmid`` and a free-text ``population``). ``literature`` is
    the optional source list used to attach a study title/indication.

    Returns a JSON-serialisable dict. The key contract for callers: the CV in
    ``powering_cv_pct`` is drawn ONLY from studies in ``studies_matching``, so
    the appendix table and the sample size cannot disagree.
    """
    target = target_population(subject_type)

    # Title/indication lookup, keyed by PMID.
    meta_by_pmid: dict[str, dict[str, Any]] = {}
    for src in literature or []:
        pmid = str(src.get("pmid") or src.get("source_pmid") or "").strip()
        if pmid:
            meta_by_pmid[pmid] = src

    # Group the flat parameter rows into one record per source study.
    grouped: dict[str, list[dict]] = {}
    for p in extracted or []:
        pmid = str(p.get("source_pmid") or "").strip() or "UNATTRIBUTED"
        grouped.setdefault(pmid, []).append(p)

    studies: list[dict[str, Any]] = []
    for pmid, rows in grouped.items():
        # A study's population is taken from its most specific row: if ANY row
        # says patient, the study is a patient study. A study cannot be
        # simultaneously a clean healthy-volunteer cohort and a patient cohort.
        classes = [classify_population(r.get("population")) for r in rows]
        if POPULATION_PATIENT in classes:
            pop_class = POPULATION_PATIENT
        elif POPULATION_SPECIAL in classes:
            pop_class = POPULATION_SPECIAL
        elif POPULATION_HEALTHY in classes:
            pop_class = POPULATION_HEALTHY
        else:
            pop_class = POPULATION_UNKNOWN

        pop_text = next(
            (str(r.get("population")).strip() for r in rows if r.get("population")),
            "",
        )

        cv_rows = [r for r in rows if r.get("parameter") in _ALL_CV_PARAMS]
        cv_candidates: list[dict[str, Any]] = []
        cv_excluded: list[dict[str, Any]] = []
        for r in cv_rows:
            v = _num(r.get("value"))
            if v is None:
                continue
            entry = {"metric": r.get("parameter"), "cv_pct": round(v, 1)}
            if v > _MAX_PLAUSIBLE_CV_PCT:
                cv_excluded.append(entry)
            else:
                cv_candidates.append(entry)

        # Within a study, the highest CV across Cmax and AUC is the conservative
        # choice -- the same convention the previous implementation used, and
        # the one regulators expect for BE powering.
        study_cv = max((c["cv_pct"] for c in cv_candidates), default=None)

        src_meta = meta_by_pmid.get(pmid, {})
        studies.append({
            "study": pmid,
            "title": src_meta.get("title") or None,
            "year": src_meta.get("year") or None,
            "indication": (
                src_meta.get("indication")
                or (pop_text if pop_class == POPULATION_PATIENT else None)
                or ("Healthy volunteers" if pop_class == POPULATION_HEALTHY else None)
            ),
            "population_text": pop_text or None,
            "population_class": pop_class,
            "auc": _fmt_value(_first_of(rows, _AUC_PARAMS)),
            "cmax": _fmt_value(_first_of(rows, _CMAX_PARAMS)),
            "cv_pct": study_cv,
            "cv_metrics": cv_candidates,
            "cv_excluded_implausible": cv_excluded,
        })

    # Stable, reviewable ordering: studies that carry a CV first (highest CV at
    # the top, since that is the one adopted), then the rest by study id.
    studies.sort(key=lambda s: (s["cv_pct"] is None, -(s["cv_pct"] or 0.0), str(s["study"])))

    matching = [s for s in studies if s["population_class"] == target]
    non_matching = [s for s in studies if s["population_class"] != target]

    cv_pool = [s for s in matching if s["cv_pct"] is not None]
    powering_cv = max((s["cv_pct"] for s in cv_pool), default=None)
    powering_study = next((s for s in cv_pool if s["cv_pct"] == powering_cv), None)

    # What a CV taken from the WRONG population would have been. Reported so the
    # protocol can show the reviewer that the difference was considered
    # deliberately, rather than leaving it looking like an oversight.
    other_pool = [s["cv_pct"] for s in non_matching if s["cv_pct"] is not None]
    other_highest = max(other_pool, default=None)

    return {
        "target_population": target,
        "subject_type": str(subject_type or "healthy"),
        "studies": studies,
        "studies_matching": matching,
        "studies_excluded_wrong_population": non_matching,
        "n_studies_total": len(studies),
        "n_studies_matching": len(matching),
        "n_studies_with_cv": len(cv_pool),
        "powering_cv_pct": powering_cv,
        "powering_cv_study": (powering_study or {}).get("study"),
        "highest_cv_other_populations_pct": other_highest,
        "summary": _summary(target, matching, cv_pool, powering_cv, other_highest, non_matching),
    }


def _summary(
    target: str,
    matching: list[dict],
    cv_pool: list[dict],
    powering_cv: Optional[float],
    other_highest: Optional[float],
    non_matching: list[dict],
) -> str:
    label = "healthy-volunteer" if target == POPULATION_HEALTHY else "patient"
    if not matching:
        return (
            f"No {label} studies with extractable PK were retrieved, so no "
            f"population-matched CV could be established; the sample size falls back "
            f"to an assumed CV, which must be declared as an assumption."
        )
    if powering_cv is None:
        return (
            f"{len(matching)} {label} study(ies) were retrieved but none reported a "
            f"within-subject CV, so the sample size rests on an assumed CV rather "
            f"than a measured one."
        )
    parts = [
        f"{len(matching)} {label} study(ies) were identified, of which "
        f"{len(cv_pool)} reported a within-subject CV. The study is powered on the "
        f"highest of these, {powering_cv:.1f}%, the conservative choice."
    ]
    if other_highest is not None and other_highest > powering_cv:
        parts.append(
            f"A higher CV of {other_highest:.1f}% was reported in "
            f"{len(non_matching)} study(ies) in a different population; it is "
            f"tabulated for completeness but is deliberately NOT used for powering, "
            f"because it does not describe the population this protocol enrols."
        )
    return " ".join(parts)


def meta_analysis_directive(result: dict[str, Any]) -> str:
    """Render the meta-analysis as LOCKED facts for the generation prompt."""
    if not result or not result.get("studies"):
        return ""
    target = result.get("target_population")
    label = "healthy volunteers" if target == POPULATION_HEALTHY else "patients"
    lines = [
        "\nMETA-ANALYSIS OF PUBLISHED PK STUDIES - ALREADY DETERMINED.",
        "Reproduce this table EXACTLY in the protocol appendix. Do NOT add studies,",
        "do NOT invent values for empty cells, and do NOT recompute the adopted CV.",
        f"- Protocol population: {label}. Only studies in this population were used",
        "  for powering; studies in other populations are listed separately and are",
        "  explicitly NOT the basis of the sample size.",
        f"- Studies retrieved: {result.get('n_studies_total')} "
        f"({result.get('n_studies_matching')} population-matched, "
        f"{result.get('n_studies_with_cv')} reporting a CV).",
    ]
    cv = result.get("powering_cv_pct")
    if cv is not None:
        lines.append(
            f"- ADOPTED within-subject CV for powering: {cv:.1f}% "
            f"(highest among population-matched studies; source {result.get('powering_cv_study')})."
        )
    else:
        lines.append(
            "- No population-matched CV was available; state plainly that the CV is "
            "an assumption, and do not attribute it to any tabulated study."
        )
    other = result.get("highest_cv_other_populations_pct")
    if other is not None and cv is not None and other > cv:
        lines.append(
            f"- NOTE: a higher CV ({other:.1f}%) exists in a non-matching population. "
            "State that it was reviewed and deliberately not adopted, with the reason."
        )
    lines.append("- Table rows (study | indication | AUC | Cmax | CV%):")
    for s in result.get("studies", []):
        flag = "" if s["population_class"] == target else "  [OTHER POPULATION - not used for powering]"
        lines.append(
            f"    {s['study']} | {s.get('indication') or 'not reported'} | "
            f"{s.get('auc') or 'not reported'} | {s.get('cmax') or 'not reported'} | "
            f"{('%.1f%%' % s['cv_pct']) if s.get('cv_pct') is not None else 'not reported'}{flag}"
        )
    lines.append(
        "  Cells reading 'not reported' MUST be rendered as such. Never fill one "
        "from prior knowledge - an invented PK value in a meta-analysis is a "
        "fabrication, not a gap."
    )
    return "\n".join(lines) + "\n"
