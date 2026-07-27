"""
Protocol risk / feasibility scoring — deterministic, no AI.

A companion to ``protocol_qc.py``. Where QC asks *"is this document internally
consistent and scientifically sound?"*, this module asks the operational
question a study manager asks: *"how hard will this protocol be to actually
run, and what could derail it?"*

Everything is derived from signals the platform already holds:

* the locked design recommendation in ``population_config["_sample_size"]``
  (N, design, within-subject CV and its provenance, NTI flag, half-life),
* the guideline version the protocol was written against,
* the QC report (unresolved failures, missing/thin sections).

Design notes
------------
* Fully deterministic and side-effect free — safe to call on every page load.
* **Never raises on missing data.** Every read goes through the ``_num`` /
  ``_get`` helpers, because a half-configured protocol is exactly the case a
  risk view needs to survive (an absent sample size is itself a risk factor).
* The score is an explainable weighted severity sum, not a black box: each
  factor contributes a fixed weight for its severity, the total is clamped to
  0-100, and the band thresholds are constants below.
"""

from __future__ import annotations

from typing import Any, Optional

from services.guidelines import is_current

# ---------------------------------------------------------------------------
# Scoring constants (kept explicit so the number is defensible to a reviewer)
# ---------------------------------------------------------------------------

# Calibrated so the bands read sensibly: one high factor alone lands at
# "moderate" (32), two high factors at "high" (64), and two moderate factors at
# "moderate" (36), while purely informational low factors never move the band.
SEVERITY_WEIGHT: dict[str, int] = {"low": 5, "moderate": 18, "high": 32}

# Band cut-offs on the 0-100 scale (higher = riskier).
MODERATE_AT = 30
HIGH_AT = 60

# Recruitment feasibility bands, on total enrolled subjects.
_N_LOW = 36
_N_MODERATE = 60
_N_HIGH = 120

# Within-subject CV bands (%).
_CV_MODERATE = 30.0
_CV_HIGH = 45.0

# A crossover needing a washout longer than this many days is operationally hard.
_WASHOUT_DAYS_HARD = 21.0


# ---------------------------------------------------------------------------
# Defensive accessors
# ---------------------------------------------------------------------------


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict OR an attribute from an ORM object, safely."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _num(value: Any) -> Optional[float]:
    """Coerce to float, or None for anything non-numeric (incl. None/'')."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _factor(label: str, severity: str, detail: str) -> dict[str, str]:
    return {"label": label, "severity": severity, "detail": detail}


def _qc_findings(qc_result: Any) -> list[dict[str, Any]]:
    """Normalise a QCReport dataclass, its ``to_dict()`` form, or None → list."""
    if qc_result is None:
        return []
    raw: Any
    if isinstance(qc_result, dict):
        raw = qc_result.get("findings") or []
    else:
        raw = getattr(qc_result, "findings", None)
        if raw is None:
            return []
    findings: list[dict[str, Any]] = []
    for f in raw or []:
        if isinstance(f, dict):
            findings.append(f)
            continue
        to_dict = getattr(f, "to_dict", None)
        if callable(to_dict):
            try:
                findings.append(to_dict())
            except Exception:  # pragma: no cover - defensive
                continue
    return findings


def _status_of(finding: dict[str, Any]) -> str:
    status = finding.get("status")
    return getattr(status, "value", status) or ""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def score_protocol_risk(protocol: Any, qc_result: Any = None) -> dict[str, Any]:
    """Score a protocol's operational feasibility risk.

    Parameters
    ----------
    protocol:
        A ``Protocol`` ORM row (or any object/dict exposing
        ``population_config``, ``sections`` and ``guideline_version``).
    qc_result:
        Optional ``QCReport`` (or its ``to_dict()``) from
        ``services.protocol_qc.run_qc``. When supplied, unresolved QC failures
        and missing sections are folded into the score instead of being
        re-derived.

    Returns
    -------
    dict with ``overall_score`` (int 0-100, higher = riskier),
    ``risk_level`` ("low" | "moderate" | "high") and ``factors``
    (list of ``{label, severity, detail}``).
    """
    pop_cfg = _get(protocol, "population_config") or {}
    if not isinstance(pop_cfg, dict):
        pop_cfg = {}
    sections = _get(protocol, "sections") or {}
    if not isinstance(sections, dict):
        sections = {}

    ss = pop_cfg.get("_sample_size") or {}
    if not isinstance(ss, dict):
        ss = {}

    factors: list[dict[str, str]] = []

    # ── 1. Recruitment feasibility (computed sample size) ─────────────────
    n_enrolled = _num(ss.get("n_enrolled"))
    n_evaluable = _num(ss.get("n_evaluable"))
    n_per_group = _num(ss.get("n_per_group"))
    n_total = n_enrolled or n_evaluable
    if n_total is None and n_per_group is not None:
        n_total = n_per_group * 2

    if n_total is None:
        factors.append(_factor(
            "No computed sample size on record", "high",
            "The design engine has not produced a sample size for this protocol, so "
            "recruitment burden, site count and budget cannot be estimated. Re-run the "
            "design step before committing to a timeline.",
        ))
    elif n_total >= _N_HIGH:
        factors.append(_factor(
            f"Large cohort: {int(n_total)} subjects", "high",
            f"{int(n_total)} subjects is a large single-site recruitment target. Expect a "
            f"long enrolment window, multiple screening waves or additional clinical sites, "
            f"and a materially higher cost than a typical {_N_LOW}-subject study.",
        ))
    elif n_total >= _N_MODERATE:
        factors.append(_factor(
            f"Sizeable cohort: {int(n_total)} subjects", "moderate",
            f"{int(n_total)} subjects is above the routine bioequivalence cohort. One clinic "
            f"can usually deliver this, but plan the screening funnel and dosing groups early.",
        ))
    elif n_total >= _N_LOW:
        factors.append(_factor(
            f"Standard cohort: {int(n_total)} subjects", "low",
            f"{int(n_total)} subjects is a routine, readily recruitable cohort for a healthy-"
            f"volunteer study.",
        ))

    # ── 2. Design complexity: replicate design and/or high variability ────
    design_key = (ss.get("design_key") or "").strip().lower() if isinstance(ss.get("design_key"), str) else ""
    design_name = ss.get("design") if isinstance(ss.get("design"), str) else ""
    if design_key == "replicate" or "replicate" in (design_name or "").lower():
        factors.append(_factor(
            "Replicate crossover design", "high",
            "A replicate design doses each subject with the reference product more than once, "
            "which means extra periods, extra washouts, more confinement days and a higher "
            "dropout exposure than a standard 2x2 crossover.",
        ))

    cv_pct = _num(ss.get("cv_pct"))
    if cv_pct is not None:
        if cv_pct >= _CV_HIGH:
            factors.append(_factor(
                f"Very high within-subject variability (CV {cv_pct:.1f}%)", "high",
                f"A within-subject CV of {cv_pct:.1f}% makes the drug highly variable: the "
                f"sample size is inflated, reference-scaling is usually required, and a "
                f"borderline result is a real possibility even for a truly equivalent product.",
            ))
        elif cv_pct >= _CV_MODERATE:
            factors.append(_factor(
                f"Elevated within-subject variability (CV {cv_pct:.1f}%)", "moderate",
                f"A within-subject CV of {cv_pct:.1f}% widens the confidence interval, so the "
                f"study has less room for dropouts or protocol deviations before power is lost.",
            ))

    # ── 3. NTI — tightened acceptance limits leave less margin ────────────
    if bool(ss.get("is_nti")):
        factors.append(_factor(
            "Narrow therapeutic index drug", "high",
            "NTI drugs must meet the tightened 90.00-111.11% acceptance limits instead of "
            "80.00-125.00%. The margin for analytical or formulation variability is roughly "
            "halved, so a manufacturing difference that would pass a normal study can fail here.",
        ))

    # ── 4. Assumed / missing PK inputs — design rests on guesses ──────────
    cv_source = ss.get("cv_source")
    cv_source_str = cv_source.strip().lower() if isinstance(cv_source, str) else ""
    if ss and (not cv_source_str or cv_source_str == "assumed"):
        factors.append(_factor(
            "Within-subject CV is assumed, not measured", "high",
            "No published within-subject CV was found for this drug, so the sample size rests "
            "on a default assumption. If the true variability is higher, the study is "
            "under-powered and may fail for statistical rather than product reasons. A pilot "
            "study or a literature override removes this risk.",
        ))
    elif cv_source_str.startswith("override"):
        factors.append(_factor(
            "Within-subject CV was manually overridden", "moderate",
            "The CV driving the sample size was entered by hand rather than taken from the "
            "literature. Keep the justification on file — a reviewer will ask for its source.",
        ))

    t_half = _num(ss.get("t_half_hours"))
    if t_half is None and ss:
        factors.append(_factor(
            "No terminal half-life available", "moderate",
            "Terminal half-life could not be extracted from the literature, so washout "
            "adequacy and the PK sampling window cannot be verified from data.",
        ))
    elif t_half is not None and design_key in ("crossover", "replicate"):
        washout_days = (7.0 * t_half) / 24.0
        if washout_days > _WASHOUT_DAYS_HARD:
            factors.append(_factor(
                f"Long washout required (~{washout_days:.0f} days)", "high",
                f"A half-life of {t_half:.1f} h implies a washout of about {washout_days:.0f} days "
                f"between periods (7 x t-half). That stretches the in-clinic calendar, raises "
                f"dropout between periods, and may make a parallel-group design the practical choice.",
            ))

    # ── 5. Guideline currency ─────────────────────────────────────────────
    guideline_version = _get(protocol, "guideline_version")
    if guideline_version:
        try:
            current = is_current(str(guideline_version))
        except Exception:  # pragma: no cover - defensive
            current = True
        if not current:
            factors.append(_factor(
                f"Superseded guideline ({guideline_version})", "high",
                "This protocol was written against a guidance document that has since been "
                "superseded. A submission built on it risks an information request or a refusal "
                "to file. Regenerate against the current guideline before finalizing.",
            ))

    # ── 6. Unresolved QC failures and missing content ─────────────────────
    findings = _qc_findings(qc_result)
    fails = [f for f in findings if _status_of(f) == "fail"]
    missing_from_qc = {
        f.get("section") for f in fails
        if str(f.get("id") or "").startswith("complete_") and f.get("section")
    }
    other_fails = [f for f in fails if not str(f.get("id") or "").startswith("complete_")]

    if other_fails:
        titles = ", ".join(str(f.get("title") or "").strip() for f in other_fails[:3] if f.get("title"))
        severity = "high" if len(other_fails) >= 3 else "moderate"
        factors.append(_factor(
            f"{len(other_fails)} unresolved QC failure{'s' if len(other_fails) != 1 else ''}",
            severity,
            f"The deterministic QC engine reports blocking issues that must be fixed before "
            f"sign-off{': ' + titles if titles else ''}. Each one is a defect a regulatory "
            f"reviewer would also catch.",
        ))

    # Missing/empty sections: prefer the QC verdict, fall back to the raw text.
    if missing_from_qc:
        empty_count = len(missing_from_qc)
    else:
        empty_count = sum(
            1 for v in sections.values()
            if not isinstance(v, str) or not v.strip()
        )
    if empty_count:
        severity = "high" if empty_count >= 3 else "moderate"
        factors.append(_factor(
            f"{empty_count} empty section{'s' if empty_count != 1 else ''}", severity,
            f"{empty_count} required section{'s are' if empty_count != 1 else ' is'} still blank. "
            f"An incomplete protocol cannot go to the ethics committee, and the gaps often hide "
            f"operational decisions (sampling, confinement, safety follow-up) nobody has made yet.",
        ))

    # A protocol with nothing flagged still deserves an explicit line.
    if not factors:
        factors.append(_factor(
            "No feasibility risks detected", "low",
            "The computed design, variability inputs, guideline version and QC results all sit "
            "inside routine ranges. Expert review is still required before sign-off.",
        ))

    # ── Aggregate ─────────────────────────────────────────────────────────
    raw_score = sum(SEVERITY_WEIGHT.get(f["severity"], 0) for f in factors)
    overall_score = max(0, min(100, int(raw_score)))
    if overall_score < MODERATE_AT:
        risk_level = "low"
    elif overall_score < HIGH_AT:
        risk_level = "moderate"
    else:
        risk_level = "high"

    return {
        "overall_score": overall_score,
        "risk_level": risk_level,
        "factors": factors,
    }
