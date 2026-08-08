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

# Within-subject (intra-individual) CV -- what a crossover or replicate design
# is powered on. This can only come from a source that measured the SAME
# subjects more than once (a crossover/replicate study), because that is the
# only way to isolate within-subject variability from between-subject noise.
_CV_INTRA_CMAX_PARAMS = ("cv_intra_cmax",)
_CV_INTRA_AUC_PARAMS = ("cv_intra_auc",)
_CV_GENERIC_PARAMS = ("cv_intra", "cv_within")
_ALL_CV_INTRA_PARAMS = _CV_INTRA_CMAX_PARAMS + _CV_INTRA_AUC_PARAMS + _CV_GENERIC_PARAMS

# Between-subject (inter-individual) CV -- what a parallel design is powered
# on. A mean +/- SD reported across a cohort measures how much subjects differ
# from EACH OTHER, never how much a single subject differs from themselves on
# repeat dosing -- there is no arithmetic that recovers a within-subject figure
# from one cohort's mean and SD. derive_cv_from_mean_sd() below therefore only
# ever emits these parameter names, never the intra ones, so a derived value
# can never be silently adopted to power a design that needs the quantity a
# single mean/SD pair is structurally unable to supply.
_CV_INTER_CMAX_PARAMS = ("cv_inter_cmax",)
_CV_INTER_AUC_PARAMS = ("cv_inter_auc",)
_ALL_CV_INTER_PARAMS = _CV_INTER_CMAX_PARAMS + _CV_INTER_AUC_PARAMS

_ALL_CV_PARAMS = _ALL_CV_INTRA_PARAMS + _ALL_CV_INTER_PARAMS

# A CV above this is implausible for a single PK metric and almost always means
# the extraction picked up the wrong quantity, a percentage that was actually
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


def derive_cv_from_mean_sd(extracted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add a deterministic cv_inter_cmax/cv_inter_auc row wherever a source gave
    a Cmax/AUC0t/AUC0inf mean with a paired SD but no directly-stated CV%.

    THE PROBLEM THIS FIXES: a CV is not always printed as a percentage. It is
    exactly as common, especially for a biologic/monoclonal-antibody label or a
    first-in-human paper, to see "mean (+/- SD) Cmax was X +/- Y" instead - and
    CV% = SD / mean x 100 is a two-number division, not a judgement call.
    Before this function existed, that division was never performed anywhere:
    extract_pk_parameters() only recorded a CV row when the source stated one
    as a percentage (per its own STRICT RULES), so a genuinely-reported,
    directly-computable CV was silently treated as absent - the sample size
    fell back to "assumed" for a drug whose literature was saying otherwise.
    This is the deterministic half of that fix: the model
    (services/ai_service.py's extraction prompt) now transcribes the mean AND
    the paired sd verbatim into the SAME row rather than doing the arithmetic
    itself - Python does the division here, exactly once, so the division is
    never delegated to the model and never silently skipped.

    WHY "inter", NEVER "intra": a mean +/- SD reported for a PK parameter is
    computed ACROSS the subjects in a cohort - it is, by construction, a
    between-subject (inter-individual) quantity. Within-subject variability
    can only be observed by dosing the SAME subject more than once (a
    crossover or replicate design) and is not recoverable from a single
    cohort's mean and SD, no matter how the arithmetic is framed. Labelling
    this derived value cv_intra_* would silently hand a between-subject number
    to code that expects a within-subject one - build_meta_analysis() and
    downstream sample-size code key off the parameter name to decide which
    quantity a CV is, so the name is the only thing standing between "this
    powers a parallel design" and "this quietly mis-powers a crossover BE
    study by roughly the square of the within/between ratio". Emitting
    cv_inter_* keeps that distinction structural rather than relying on every
    caller to remember it.

    Runs BEFORE build_meta_analysis(), which is the only thing that reads
    cv_inter_cmax/cv_inter_auc (or the within-subject cv_intra_*/cv_intra/
    cv_within families) - a derived row is indistinguishable to that function
    from a directly-stated one except for the ``derived_from_mean_sd`` flag
    added here, which build_meta_analysis carries through to ``cv_metrics`` so
    the appendix directive can disclose the provenance honestly rather than
    presenting a derived figure as if the source had printed a percentage.

    Conservative by construction: a PMID that already carries an explicit CV
    for a given metric (within- or between-subject, or a generic,
    metric-unspecified cv_intra/cv_within) is left alone for THAT metric - an
    explicit value always wins over a derived one, never the reverse. Returns
    a NEW list (``extracted`` is not mutated); returns it unchanged (same
    object) when there is nothing to derive, so a caller with no mean/SD pairs
    pays no cost.
    """
    explicit_cmax_pmids: set[str] = set()
    explicit_auc_pmids: set[str] = set()
    for p in extracted:
        pmid = str(p.get("source_pmid") or "")
        param = p.get("parameter")
        if param in _CV_INTRA_CMAX_PARAMS or param in _CV_INTER_CMAX_PARAMS or param in _CV_GENERIC_PARAMS:
            explicit_cmax_pmids.add(pmid)
        if param in _CV_INTRA_AUC_PARAMS or param in _CV_INTER_AUC_PARAMS or param in _CV_GENERIC_PARAMS:
            explicit_auc_pmids.add(pmid)

    derived: list[dict[str, Any]] = []
    for p in extracted:
        param = p.get("parameter")
        if param not in _CMAX_PARAMS and param not in _AUC_PARAMS:
            continue
        mean = _num(p.get("value"))
        sd = _num(p.get("sd"))
        if mean is None or sd is None:
            continue
        pmid = str(p.get("source_pmid") or "")
        is_cmax = param in _CMAX_PARAMS
        if is_cmax and pmid in explicit_cmax_pmids:
            continue
        if not is_cmax and pmid in explicit_auc_pmids:
            continue
        cv_pct = round((sd / mean) * 100.0, 1)
        derived.append({
            "parameter": "cv_inter_cmax" if is_cmax else "cv_inter_auc",
            "value": cv_pct,
            "unit": "%",
            "population": p.get("population"),
            "source_pmid": p.get("source_pmid"),
            # Slightly below a directly-stated value's typical 1.0, since this
            # is arithmetic on numbers the model transcribed rather than a
            # percentage the source printed outright.
            "confidence": min(_num(p.get("confidence")) or 0.9, 0.85),
            "derived_from_mean_sd": True,
            "derived_from_parameter": param,
        })

    if not derived:
        return extracted
    return list(extracted) + derived


def build_meta_analysis(
    extracted: list[dict[str, Any]],
    subject_type: Optional[str] = "healthy",
    literature: Optional[list[dict[str, Any]]] = None,
    cv_basis: str = "within",
) -> dict[str, Any]:
    """Build the per-study meta-analysis table and pick the powering CV.

    ``extracted`` is the flat list of PK parameters (one row per parameter, each
    carrying ``source_pmid`` and a free-text ``population``). ``literature`` is
    the optional source list used to attach a study title/indication.

    ``cv_basis`` states which variance quantity THIS protocol's sample size
    actually needs: ``"within"`` (within-subject/intra-subject -- crossover and
    replicate designs) or ``"between"`` (between-subject/total variability --
    parallel designs, and specifically the biosimilar-parallel case which uses
    the resolved CV directly with no within-to-between conversion; see
    services/biostatistics.py's own ``cv_for_power`` logic). A study's CV
    metrics of the WRONG basis for ``cv_basis`` are tabulated under
    ``cv_wrong_basis_metrics`` -- so the appendix can disclose what was found
    and explain why it was not used -- but they never contribute to
    ``powering_cv_pct``, exactly mirroring how a wrong-population CV is
    tabulated but excluded from powering.

    Returns a JSON-serialisable dict. The key contract for callers: the CV in
    ``powering_cv_pct`` is drawn ONLY from studies in ``studies_matching`` AND
    only from basis-matching metrics, so the appendix table and the sample
    size cannot disagree.
    """
    if cv_basis not in ("within", "between"):
        raise ValueError(f"cv_basis must be 'within' or 'between', got {cv_basis!r}")
    basis_params = _ALL_CV_INTRA_PARAMS if cv_basis == "within" else _ALL_CV_INTER_PARAMS
    off_basis_params = _ALL_CV_INTER_PARAMS if cv_basis == "within" else _ALL_CV_INTRA_PARAMS
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

        cv_rows = [r for r in rows if r.get("parameter") in basis_params]
        cv_candidates: list[dict[str, Any]] = []
        cv_excluded: list[dict[str, Any]] = []
        for r in cv_rows:
            v = _num(r.get("value"))
            if v is None:
                continue
            entry = {
                "metric": r.get("parameter"),
                "cv_pct": round(v, 1),
                "derived_from_mean_sd": bool(r.get("derived_from_mean_sd")),
            }
            if v > _MAX_PLAUSIBLE_CV_PCT:
                cv_excluded.append(entry)
            else:
                cv_candidates.append(entry)

        # A CV of the WRONG basis is tabulated for disclosure but never feeds
        # study_cv -- the same "found it, but it does not answer the question
        # this protocol is asking" treatment as a wrong-population CV.
        off_basis_rows = [r for r in rows if r.get("parameter") in off_basis_params]
        off_basis_candidates: list[dict[str, Any]] = []
        for r in off_basis_rows:
            v = _num(r.get("value"))
            if v is None:
                continue
            off_basis_candidates.append({
                "metric": r.get("parameter"),
                "cv_pct": round(v, 1),
                "derived_from_mean_sd": bool(r.get("derived_from_mean_sd")),
            })

        # Within a study, the highest CV across Cmax and AUC is the conservative
        # choice -- the same convention the previous implementation used, and
        # the one regulators expect for BE powering.
        study_cv = max((c["cv_pct"] for c in cv_candidates), default=None)
        study_cv_wrong_basis = max((c["cv_pct"] for c in off_basis_candidates), default=None)

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
            "cv_wrong_basis_pct": study_cv_wrong_basis,
            "cv_wrong_basis_metrics": off_basis_candidates,
        })

    # Stable, reviewable ordering: studies that carry a CV first (highest CV at
    # the top, since that is the one adopted), then the rest by study id.
    studies.sort(key=lambda s: (s["cv_pct"] is None, -(s["cv_pct"] or 0.0), str(s["study"])))

    matching = [s for s in studies if s["population_class"] == target]
    non_matching = [s for s in studies if s["population_class"] != target]

    cv_pool = [s for s in matching if s["cv_pct"] is not None]
    powering_cv = max((s["cv_pct"] for s in cv_pool), default=None)
    powering_study = next((s for s in cv_pool if s["cv_pct"] == powering_cv), None)
    # Whether the SPECIFIC metric that set powering_cv was a derived (mean/SD)
    # value rather than a directly-stated percentage — the directive discloses
    # this distinction rather than presenting a derived figure as if the
    # source itself had printed a CV%.
    powering_cv_derived = False
    if powering_study is not None:
        powering_cv_derived = any(
            m["cv_pct"] == powering_cv and m.get("derived_from_mean_sd")
            for m in powering_study.get("cv_metrics", [])
        )

    # What a CV taken from the WRONG population would have been. Reported so the
    # protocol can show the reviewer that the difference was considered
    # deliberately, rather than leaving it looking like an oversight.
    other_pool = [s["cv_pct"] for s in non_matching if s["cv_pct"] is not None]
    other_highest = max(other_pool, default=None)

    # What a CV of the WRONG basis (within vs. between-subject) would have
    # been, among population-matched studies. Reported for the same reason as
    # other_highest: so the reviewer sees the omission was deliberate.
    wrong_basis_pool = [
        s["cv_wrong_basis_pct"] for s in matching if s.get("cv_wrong_basis_pct") is not None
    ]
    wrong_basis_highest = max(wrong_basis_pool, default=None)

    return {
        "target_population": target,
        "subject_type": str(subject_type or "healthy"),
        "cv_basis": cv_basis,
        "studies": studies,
        "studies_matching": matching,
        "studies_excluded_wrong_population": non_matching,
        "n_studies_total": len(studies),
        "n_studies_matching": len(matching),
        "n_studies_with_cv": len(cv_pool),
        "powering_cv_pct": powering_cv,
        "powering_cv_study": (powering_study or {}).get("study"),
        "powering_cv_derived_from_mean_sd": powering_cv_derived,
        "highest_cv_other_populations_pct": other_highest,
        "highest_cv_wrong_basis_pct": wrong_basis_highest,
        "summary": _summary(
            target, matching, cv_pool, powering_cv, other_highest, non_matching,
            powering_cv_derived, cv_basis, wrong_basis_highest,
        ),
    }


def _summary(
    target: str,
    matching: list[dict],
    cv_pool: list[dict],
    powering_cv: Optional[float],
    other_highest: Optional[float],
    non_matching: list[dict],
    powering_cv_derived: bool = False,
    cv_basis: str = "within",
    wrong_basis_highest: Optional[float] = None,
) -> str:
    label = "healthy-volunteer" if target == POPULATION_HEALTHY else "patient"
    basis_label = "within-subject" if cv_basis == "within" else "between-subject (total)"
    if not matching:
        return (
            f"No {label} studies with extractable PK were retrieved, so no "
            f"population-matched CV could be established; the sample size falls back "
            f"to an assumed CV, which must be declared as an assumption."
        )
    if powering_cv is None:
        msg = (
            f"{len(matching)} {label} study(ies) were retrieved but none reported or "
            f"gave a derivable {basis_label} CV, so the sample size rests on an "
            f"assumed CV rather than a measured one."
        )
        if wrong_basis_highest is not None:
            msg += (
                f" A {('between-subject' if cv_basis == 'within' else 'within-subject')} "
                f"CV of {wrong_basis_highest:.1f}% was found, but it is not the quantity "
                f"this design is powered on and was deliberately NOT used."
            )
        return msg
    basis = (
        "derived from that study's reported mean and standard deviation "
        "(CV% = SD/mean x 100), not a percentage the study stated directly"
        if powering_cv_derived else
        "the conservative choice"
    )
    parts = [
        f"{len(matching)} {label} study(ies) were identified, of which "
        f"{len(cv_pool)} reported or gave a derivable {basis_label} CV. The study is "
        f"powered on the highest of these, {powering_cv:.1f}%, {basis}."
    ]
    if other_highest is not None and other_highest > powering_cv:
        parts.append(
            f"A higher CV of {other_highest:.1f}% was reported in "
            f"{len(non_matching)} study(ies) in a different population; it is "
            f"tabulated for completeness but is deliberately NOT used for powering, "
            f"because it does not describe the population this protocol enrols."
        )
    if wrong_basis_highest is not None:
        parts.append(
            f"A {('between-subject' if cv_basis == 'within' else 'within-subject')} CV "
            f"of {wrong_basis_highest:.1f}% was also found among population-matched "
            f"studies; it is tabulated for completeness but is deliberately NOT used "
            f"for powering, because it is not the variability quantity this design "
            f"requires."
        )
    return " ".join(parts)


def meta_analysis_directive(result: dict[str, Any]) -> str:
    """Render the meta-analysis as LOCKED facts for the generation prompt."""
    if not result or not result.get("studies"):
        return ""
    target = result.get("target_population")
    label = "healthy volunteers" if target == POPULATION_HEALTHY else "patients"
    cv_basis = result.get("cv_basis") or "within"
    basis_label = "within-subject" if cv_basis == "within" else "between-subject (total)"
    other_basis_label = "between-subject" if cv_basis == "within" else "within-subject"
    lines = [
        "\nMETA-ANALYSIS OF PUBLISHED PK STUDIES - ALREADY DETERMINED.",
        "Reproduce this table EXACTLY in the protocol appendix. Do NOT add studies,",
        "do NOT invent values for empty cells, and do NOT recompute the adopted CV.",
        f"- Protocol population: {label}. Only studies in this population were used",
        "  for powering; studies in other populations are listed separately and are",
        "  explicitly NOT the basis of the sample size.",
        f"- This design is powered on {basis_label} variability, so only CVs of that "
        f"  basis were eligible for adoption; a {other_basis_label} CV, even from a "
        "  population-matched study, cannot substitute for it and is disclosed but not used.",
        f"- Studies retrieved: {result.get('n_studies_total')} "
        f"({result.get('n_studies_matching')} population-matched, "
        f"{result.get('n_studies_with_cv')} reporting a CV of the required basis).",
    ]
    cv = result.get("powering_cv_pct")
    if cv is not None:
        provenance = (
            "derived from that study's reported mean and SD (CV% = SD/mean x 100), "
            "not a percentage the study printed directly — state this honestly rather "
            "than describing it as reported"
            if result.get("powering_cv_derived_from_mean_sd") else
            "as directly reported by that study"
        )
        lines.append(
            f"- ADOPTED {basis_label} CV for powering: {cv:.1f}% "
            f"(highest among population-matched studies; source {result.get('powering_cv_study')}, "
            f"{provenance})."
        )
    else:
        lines.append(
            f"- No population-matched {basis_label} CV was available; state plainly "
            "that the CV is an assumption, and do not attribute it to any tabulated study."
        )
    other = result.get("highest_cv_other_populations_pct")
    if other is not None and cv is not None and other > cv:
        lines.append(
            f"- NOTE: a higher CV ({other:.1f}%) exists in a non-matching population. "
            "State that it was reviewed and deliberately not adopted, with the reason."
        )
    wrong_basis = result.get("highest_cv_wrong_basis_pct")
    if wrong_basis is not None:
        lines.append(
            f"- NOTE: a {other_basis_label} CV ({wrong_basis:.1f}%) was found in a "
            "population-matched study. It is NOT the variability quantity this design "
            "is powered on and must NOT be substituted for the adopted CV above — "
            "state that it was reviewed and deliberately not adopted, with the reason."
        )
    lines.append("- Table rows (study | indication | AUC | Cmax | CV%):")
    for s in result.get("studies", []):
        flag = "" if s["population_class"] == target else "  [OTHER POPULATION - not used for powering]"
        cv_cell = "not reported"
        if s.get("cv_pct") is not None:
            row_derived = any(
                m["cv_pct"] == s["cv_pct"] and m.get("derived_from_mean_sd")
                for m in s.get("cv_metrics", [])
            )
            cv_cell = "%.1f%%%s" % (s["cv_pct"], " (derived from mean/SD)" if row_derived else "")
        elif s.get("cv_wrong_basis_pct") is not None:
            cv_cell = (
                "not reported (%.1f%% %s CV found, not usable for this design)"
                % (s["cv_wrong_basis_pct"], other_basis_label)
            )
        lines.append(
            f"    {s['study']} | {s.get('indication') or 'not reported'} | "
            f"{s.get('auc') or 'not reported'} | {s.get('cmax') or 'not reported'} | "
            f"{cv_cell}{flag}"
        )
    lines.append(
        "  Cells reading 'not reported' MUST be rendered as such. Never fill one "
        "from prior knowledge - an invented PK value in a meta-analysis is a "
        "fabrication, not a gap. A CV cell marked '(derived from mean/SD)' was NOT "
        "printed as a percentage by that study - it was computed here from the "
        "study's own reported mean and standard deviation. State it as derived, not "
        "as a value the study reported outright."
    )
    return "\n".join(lines) + "\n"
