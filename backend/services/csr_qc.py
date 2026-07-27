"""Deterministic QC for a Clinical Study Report (no AI).

Reconciles the computed results against the study's own design and flags the
things a reviewer must check before a CSR is finalised: enough subjects analysed
vs. planned, the co-primary endpoints actually computable, an unambiguous
verdict, high terminal extrapolation, and observed vs. assumed variability.
Mirrors the shape of :mod:`services.protocol_qc` so the UI can render it the same
way.
"""
from __future__ import annotations

from typing import Any

_CO_PRIMARY = ("cmax", "auc0inf")


def _finding(fid: str, category: str, title: str, status: str, detail: str, section: str | None = None) -> dict:
    return {"id": fid, "category": category, "title": title, "status": status, "detail": detail, "section": section}


def run_csr_qc(results: dict, protocol: Any = None) -> dict:
    """Return {submission_ready, counts, total_checks, findings[]} for a CSR's
    locked results. ``protocol`` (optional) supplies the planned design to compare
    against."""
    findings: list[dict] = []
    be = results.get("be") or {}
    disp = results.get("disposition") or {}
    ss = (getattr(protocol, "population_config", None) or {}).get("_sample_size") or {} if protocol is not None else {}

    # 1. Verdict present and unambiguous.
    verdict = results.get("verdict")
    if verdict == "bioequivalent":
        findings.append(_finding("verdict", "conclusion", "Bioequivalence conclusion",
                                 "pass", "Both co-primary endpoints fall within the acceptance limits."))
    elif verdict == "not_bioequivalent":
        findings.append(_finding("verdict", "conclusion", "Bioequivalence conclusion",
                                 "warning", "At least one co-primary endpoint fell outside the acceptance limits — confirm before reporting."))
    else:
        findings.append(_finding("verdict", "conclusion", "Bioequivalence conclusion",
                                 "fail", "Verdict is indeterminate — a co-primary endpoint could not be analysed (see below)."))

    # 2. Each co-primary endpoint computable.
    for m in _CO_PRIMARY:
        r = be.get(m)
        if not isinstance(r, dict) or "error" in (r or {}):
            reason = (r or {}).get("error", "not analysed")
            findings.append(_finding(f"coprimary_{m}", "statistics", f"Co-primary endpoint {m.upper()}",
                                     "fail", f"{m.upper()} could not be analysed: {reason}.", "bioequivalence_assessment"))
        else:
            findings.append(_finding(f"coprimary_{m}", "statistics", f"Co-primary endpoint {m.upper()}",
                                     "pass", f"{m.upper()} GMR {r['point_estimate_pct']:.2f}% "
                                             f"(90% CI {r['ci_lower_pct']:.2f}–{r['ci_upper_pct']:.2f}%).",
                                     "bioequivalence_assessment"))

    # 3. Analysed N vs planned N.
    analysed = disp.get("n_subjects")
    planned = ss.get("n_enrolled")
    if analysed is None:
        findings.append(_finding("n_analysed", "population", "Subjects analysed", "warning",
                                 "Number of analysed subjects is unknown.", "study_population"))
    elif planned and analysed < planned:
        findings.append(_finding("n_analysed", "population", "Subjects analysed vs planned", "warning",
                                 f"{analysed} subjects analysed vs {planned} planned for enrolment — "
                                 f"confirm the study retained adequate power.", "study_population"))
    else:
        findings.append(_finding("n_analysed", "population", "Subjects analysed", "pass",
                                 f"{analysed} subjects contributed to the analysis.", "study_population"))

    # 4. Observed vs assumed within-subject CV (from the primary Cmax result).
    cmax = be.get("cmax")
    if isinstance(cmax, dict) and "cv_intra_pct" in cmax and ss.get("cv_pct"):
        obs, assumed = cmax["cv_intra_pct"], ss["cv_pct"]
        if obs > assumed * 1.25:
            findings.append(_finding("cv_obs", "statistics", "Observed variability higher than assumed", "warning",
                                     f"Observed intra-subject CV {obs:.1f}% exceeds the {assumed:.1f}% assumed at "
                                     f"design — the study may have been under-powered.", "bioequivalence_assessment"))
        else:
            findings.append(_finding("cv_obs", "statistics", "Observed vs assumed variability", "pass",
                                     f"Observed intra-subject CV {obs:.1f}% is consistent with the {assumed:.1f}% "
                                     f"assumed at design.", "bioequivalence_assessment"))

    # 5. High terminal extrapolation on NCA (AUC0-inf reliability).
    nca = results.get("nca")
    if isinstance(nca, dict) and nca:
        high = [k for k, v in nca.items() if isinstance(v, dict) and (v.get("pct_extrap") or 0) > 20]
        if high:
            findings.append(_finding("extrap", "pharmacokinetics", "High AUC extrapolation", "warning",
                                     f"{len(high)} profile(s) have %AUCextrap > 20% — AUC0-inf is less reliable for these; "
                                     f"consider AUC0-t as the primary AUC metric.", "pharmacokinetic_results"))
        else:
            findings.append(_finding("extrap", "pharmacokinetics", "AUC extrapolation", "pass",
                                     "All profiles have acceptable (<20%) terminal extrapolation.", "pharmacokinetic_results"))

    counts: dict[str, int] = {"pass": 0, "warning": 0, "fail": 0}
    for f in findings:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    return {
        "submission_ready": counts["fail"] == 0,
        "counts": counts,
        "total_checks": len(findings),
        "findings": findings,
    }
