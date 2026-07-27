"""
Protocol QC — deterministic internal-consistency and scientific-soundness checks.

Runs WITHOUT any AI call (like ``biostatistics.py``): given the stored
``population_config`` (which carries the locked ``_sample_size`` recommendation
and ``_label`` data) and the generated ``sections`` text, it produces a
reviewer-grade checklist. The goal is to surface every inconsistency or
assumption a regulatory reviewer would otherwise catch — turning an AI draft
into an auditable one.

Design notes
------------
* Hard checks (e.g. acceptance limits vs approach) can FAIL; soft text-scan
  checks (e.g. washout phrasing) are conservative and prefer WARNING to avoid
  false positives on free-form prose.
* The sample-size *drift* check is the headline: the engine computes N once and
  locks it; this proves the narrative never contradicts it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from services.gold_standard import (
    applicable_content_features,
    applicable_deterministic_features,
)


class QCStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"        # likely issue — human should verify
    FAIL = "fail"              # definite inconsistency — must resolve
    ASSUMPTION = "assumption"  # a default assumption requiring sign-off
    NA = "not_applicable"


# Severity ordering for sorting (worst first) and summary readiness.
_SEVERITY = {
    QCStatus.FAIL: 0,
    QCStatus.ASSUMPTION: 1,
    QCStatus.WARNING: 2,
    QCStatus.PASS: 3,
    QCStatus.NA: 4,
}


@dataclass
class QCFinding:
    id: str
    category: str            # "Sample size" | "Statistics" | "PK & design" | "Completeness" | "Regulatory"
    title: str
    status: QCStatus
    detail: str
    section: Optional[str] = None  # related section key, if any

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class QCReport:
    findings: list[QCFinding] = field(default_factory=list)

    # ---- summary ---------------------------------------------------------
    @property
    def counts(self) -> dict[str, int]:
        c = {s.value: 0 for s in QCStatus}
        for f in self.findings:
            c[f.status.value] += 1
        return c

    @property
    def submission_ready(self) -> bool:
        """True when there are no hard failures to resolve."""
        return not any(f.status is QCStatus.FAIL for f in self.findings)

    def sorted_findings(self) -> list[QCFinding]:
        return sorted(self.findings, key=lambda f: (_SEVERITY[f.status], f.category))

    def to_dict(self) -> dict[str, Any]:
        c = self.counts
        return {
            "submission_ready": self.submission_ready,
            "counts": c,
            "total_checks": len(self.findings),
            "findings": [f.to_dict() for f in self.sorted_findings()],
        }


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------

_PLAUSIBLE_N = range(4, 601)
_UNIT_WORDS = ("hour", "day", "week", "month", "year", "mg", "ml", "%", "ng", "mcg", "kg")


def _txt(sections: dict[str, str], key: str) -> str:
    return (sections.get(key) or "").strip()


def _subject_counts(text: str) -> set[int]:
    """Best-effort extraction of integers that clearly denote a subject count.

    Conservative by design: an integer only counts if it is adjacent to an
    enrolment keyword or a subject noun, so ages ("18 to 55 years"), visit days
    ("Day 29") and doses ("400 mg") are not mistaken for sample sizes.
    """
    found: set[int] = set()
    if not text:
        return found

    patterns = [
        # "40 (healthy adult) subjects/volunteers/participants/patients"
        r"\b(\d{1,3})\s+(?:[A-Za-z]+\s+){0,3}?(?:subjects|volunteers|participants|patients|individuals)\b",
        # "sample size of 40", "a total of 40", "N = 40"
        r"\bsample\s+size\s+(?:of|was|is|=|:)?\s*(\d{1,3})\b",
        r"\btotal\s+of\s+(\d{1,3})\b",
        r"\bN\s*=\s*(\d{1,3})\b",
        # "enroll 40", "randomized 40", "recruit 40"
        r"\b(?:enroll(?:ed|ment of|ing)?|randomi[sz]ed?|recruit(?:ed|ing)?)\s+(?:a\s+total\s+of\s+)?(\d{1,3})\b",
    ]
    lowered = text.lower()
    for pat in patterns:
        for m in re.finditer(pat, lowered):
            # guard: skip if a unit word sits in the matched span (e.g. "24 hour ... subjects")
            span = lowered[m.start():m.end()]
            if any(u in span for u in _UNIT_WORDS):
                continue
            try:
                v = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if v in _PLAUSIBLE_N:
                found.add(v)
    return found


def _has_any(text: str, *tokens: str) -> bool:
    low = text.lower()
    return any(t.lower() in low for t in tokens)


def _find_washout_hours(text: str) -> Optional[float]:
    """Parse a washout duration from prose; returns hours or None."""
    if not text:
        return None
    low = text.lower()
    # "washout (period) of (at least) 7 days/weeks/hours"  OR  "7-day washout"
    pats = [
        r"washout\s+(?:period\s+)?(?:of\s+)?(?:at\s+least\s+)?(\d{1,3})\s*(hours?|h|days?|weeks?)",
        r"(\d{1,3})\s*-?\s*(hours?|h|days?|weeks?)\s+washout",
    ]
    for pat in pats:
        m = re.search(pat, low)
        if m:
            n = float(m.group(1))
            unit = m.group(2)
            if unit.startswith(("hour", "h")):
                return n
            if unit.startswith("day"):
                return n * 24
            if unit.startswith("week"):
                return n * 24 * 7
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# Minimum content length for a section to count as "drafted" (title is shorter).
_MIN_CHARS = 180
_MIN_CHARS_OVERRIDE = {"title": 60, "references": 80}


def run_qc(
    *,
    study_type: str,
    population_config: dict[str, Any],
    sections: dict[str, str],
    expected_sections: list[str],
    section_labels: Optional[dict[str, str]] = None,
) -> QCReport:
    labels = section_labels or {}
    report = QCReport()
    add = report.findings.append

    ss: dict[str, Any] = population_config.get("_sample_size") or {}
    label: dict[str, Any] = population_config.get("_label") or {}

    def label_of(key: str) -> str:
        return labels.get(key, key)

    # ── 1. Completeness ───────────────────────────────────────────────────
    for name in expected_sections:
        body = _txt(sections, name)
        floor = _MIN_CHARS_OVERRIDE.get(name, _MIN_CHARS)
        if not body:
            add(QCFinding(
                id=f"complete_{name}", category="Completeness",
                title=f"Missing section: {label_of(name)}", status=QCStatus.FAIL,
                detail="This section is required for the study type but is empty.",
                section=name,
            ))
        elif len(body) < floor:
            add(QCFinding(
                id=f"complete_{name}", category="Completeness",
                title=f"Thin section: {label_of(name)}", status=QCStatus.WARNING,
                detail=f"Only {len(body)} characters — likely under-developed.",
                section=name,
            ))
    # one positive line so the report isn't all-or-nothing
    drafted = sum(1 for n in expected_sections if len(_txt(sections, n)) >= _MIN_CHARS_OVERRIDE.get(n, _MIN_CHARS))
    add(QCFinding(
        id="complete_overall", category="Completeness",
        title=f"{drafted}/{len(expected_sections)} sections drafted",
        status=QCStatus.PASS if drafted == len(expected_sections) else QCStatus.WARNING,
        detail="All required sections contain substantive content."
        if drafted == len(expected_sections) else "Some sections need more content.",
    ))

    # ── 2. Sample-size consistency (headline) ─────────────────────────────
    if ss:
        n_eval = ss.get("n_evaluable")
        n_enr = ss.get("n_enrolled")
        n_pg = ss.get("n_per_group")
        allowed: set[int] = set()
        for v in (n_eval, n_enr):
            if isinstance(v, int):
                allowed.add(v)
                allowed.add(v // 2)            # per-sequence in a 2×2 crossover
        if isinstance(n_pg, int):
            allowed.update({n_pg, 2 * n_pg})

        scan_keys = ["synopsis", "study_design", "population", "statistical_analysis_plan"]
        discrepancies: dict[int, list[str]] = {}
        locked_seen = False
        for k in scan_keys:
            for v in _subject_counts(_txt(sections, k)):
                if v in allowed:
                    if v in (n_eval, n_enr):
                        locked_seen = True
                elif v not in (0,):
                    discrepancies.setdefault(v, []).append(k)

        n_label = f"N={n_enr} enrolled ({n_eval} evaluable)" if n_enr else "the computed N"
        if discrepancies:
            disc_str = "; ".join(
                f"{v} (in {', '.join(label_of(s) for s in secs)})"
                for v, secs in sorted(discrepancies.items())
            )
            add(QCFinding(
                id="ss_drift", category="Sample size",
                title="Sample size disagreement across sections", status=QCStatus.FAIL,
                detail=f"Locked sample size is {n_label}, but the narrative also "
                       f"mentions: {disc_str}. Every section must cite the locked N.",
                section="statistical_analysis_plan",
            ))
        elif not locked_seen:
            add(QCFinding(
                id="ss_surfaced", category="Sample size",
                title="Locked N not stated in the narrative", status=QCStatus.WARNING,
                detail=f"The engine locked {n_label} but it was not found in the "
                       f"synopsis or SAP text. Confirm the number is written out.",
                section="statistical_analysis_plan",
            ))
        else:
            add(QCFinding(
                id="ss_consistent", category="Sample size",
                title="Sample size consistent across sections", status=QCStatus.PASS,
                detail=f"{n_label} is stated consistently and no conflicting count was found.",
                section="statistical_analysis_plan",
            ))
    else:
        add(QCFinding(
            id="ss_missing", category="Sample size",
            title="No computed sample size on record", status=QCStatus.WARNING,
            detail="population_config has no _sample_size block — was the design engine run?",
        ))

    # ── 3. Statistics: acceptance limits, CV provenance, assumptions ──────
    sap = _txt(sections, "statistical_analysis_plan")
    if ss and sap:
        is_nti = bool(ss.get("is_nti"))
        needs_sim = bool(ss.get("needs_simulation"))
        if is_nti:
            if _has_any(sap, "90.00", "111.11", "111.1"):
                add(QCFinding(id="limits", category="Statistics",
                    title="NTI acceptance limits correct", status=QCStatus.PASS,
                    detail="SAP states the tightened 90.00–111.11% limits for the NTI drug.",
                    section="statistical_analysis_plan"))
            elif _has_any(sap, "80.00", "125.00") or ("80" in sap and "125" in sap):
                add(QCFinding(id="limits", category="Statistics",
                    title="Wrong acceptance limits for an NTI drug", status=QCStatus.FAIL,
                    detail="Drug is flagged NTI but the SAP states the standard 80.00–125.00% "
                           "limits instead of the required 90.00–111.11%.",
                    section="statistical_analysis_plan"))
            else:
                add(QCFinding(id="limits", category="Statistics",
                    title="NTI limits not clearly stated", status=QCStatus.WARNING,
                    detail="Could not confirm the 90.00–111.11% limits in the SAP.",
                    section="statistical_analysis_plan"))
        elif needs_sim:
            if _has_any(sap, "reference-scaled", "reference scaled", "scaled", "widened", "RSABE", "swr"):
                add(QCFinding(id="limits", category="Statistics",
                    title="Reference-scaling described", status=QCStatus.PASS,
                    detail="SAP describes reference-scaled average BE for the highly-variable drug.",
                    section="statistical_analysis_plan"))
            else:
                add(QCFinding(id="limits", category="Statistics",
                    title="Reference-scaling not described", status=QCStatus.WARNING,
                    detail="Highly-variable drug (RSABE) but the SAP does not mention scaled/widened limits.",
                    section="statistical_analysis_plan"))
        else:
            if "125" in sap and ("80.00" in sap or "80%" in sap or "80 " in sap or "0.80" in sap):
                add(QCFinding(id="limits", category="Statistics",
                    title="Standard BE limits correct", status=QCStatus.PASS,
                    detail="SAP states the 80.00–125.00% acceptance limits.",
                    section="statistical_analysis_plan"))
            else:
                add(QCFinding(id="limits", category="Statistics",
                    title="Acceptance limits not clearly stated", status=QCStatus.WARNING,
                    detail="Could not confirm the 80.00–125.00% limits in the SAP text.",
                    section="statistical_analysis_plan"))

        # power & GMR stated
        power_pct = round(float(ss.get("power", 0)) * 100)
        if power_pct and (f"{power_pct}%" in sap or f"{power_pct} %" in sap or f"{power_pct/100:.2f}" in sap):
            pass  # fine
        else:
            add(QCFinding(id="power_stated", category="Statistics",
                title="Target power not stated in SAP", status=QCStatus.WARNING,
                detail=f"The design targets {power_pct}% power; state it explicitly in the SAP.",
                section="statistical_analysis_plan"))

    # CV provenance / assumption sign-off
    if ss:
        cv = ss.get("cv_pct")
        src = ss.get("cv_source")
        if src == "assumed":
            add(QCFinding(id="cv_assumption", category="Statistics",
                title="Within-subject CV is an assumption", status=QCStatus.ASSUMPTION,
                detail=f"CV of {cv:.1f}% was assumed (no literature value found). This drives the "
                       f"sample size — confirm or override before finalizing.",
                section="statistical_analysis_plan"))
        elif src in ("literature", "override") and cv is not None:
            add(QCFinding(id="cv_source", category="Statistics",
                title=f"Within-subject CV {cv:.1f}% ({src})", status=QCStatus.PASS,
                detail="CV is sourced rather than guessed." if src == "literature"
                       else "CV was explicitly provided.",
                section="statistical_analysis_plan"))

    # ── 4. PK & design: washout vs half-life ──────────────────────────────
    if ss:
        design_key = ss.get("design_key")
        t_half = ss.get("t_half_hours")
        if design_key == "parallel":
            add(QCFinding(id="washout", category="PK & design",
                title="Washout not applicable (parallel design)", status=QCStatus.NA,
                detail="A parallel-group design carries no washout requirement.",
                section="study_design"))
        elif design_key == "crossover" and t_half:
            req = 7 * t_half
            text = _txt(sections, "treatment") + "\n" + _txt(sections, "study_design")
            wo = _find_washout_hours(text)
            if wo is None:
                add(QCFinding(id="washout", category="PK & design",
                    title="Washout duration not detected", status=QCStatus.WARNING,
                    detail=f"Half-life ≈ {t_half:.1f} h implies a washout of ≥ {req/24:.1f} days "
                           f"(7 × t½). No explicit washout was found in Treatment/Study Design.",
                    section="treatment"))
            elif wo >= req:
                add(QCFinding(id="washout", category="PK & design",
                    title="Washout adequate (≥ 7 half-lives)", status=QCStatus.PASS,
                    detail=f"Stated washout ≈ {wo/24:.1f} days ≥ required {req/24:.1f} days.",
                    section="treatment"))
            elif wo >= 5 * t_half:
                add(QCFinding(id="washout", category="PK & design",
                    title="Washout marginal (5–7 half-lives)", status=QCStatus.WARNING,
                    detail=f"Stated washout ≈ {wo/24:.1f} days is below the conservative "
                           f"7×t½ = {req/24:.1f} days. Justify or extend.",
                    section="treatment"))
            else:
                add(QCFinding(id="washout", category="PK & design",
                    title="Washout too short — carryover risk", status=QCStatus.FAIL,
                    detail=f"Stated washout ≈ {wo/24:.1f} days is well under 7×t½ = {req/24:.1f} days "
                           f"(t½ ≈ {t_half:.1f} h). Carryover would confound the crossover.",
                    section="treatment"))
            # impractical-crossover flag
            if req / 24 > 21:
                add(QCFinding(id="design_feasibility", category="PK & design",
                    title="Long half-life vs crossover feasibility", status=QCStatus.WARNING,
                    detail=f"t½ ≈ {t_half:.1f} h needs a ≥ {req/24:.0f}-day washout; verify a crossover "
                           f"is practical or consider a parallel design.",
                    section="study_design"))
        elif design_key == "crossover" and not t_half:
            add(QCFinding(id="washout", category="PK & design",
                title="Cannot verify washout (no half-life)", status=QCStatus.WARNING,
                detail="No terminal half-life was available, so washout adequacy can't be checked.",
                section="study_assessments"))

    # ── 5. PK sampling adequacy (light) ───────────────────────────────────
    pk = _txt(sections, "study_assessments")
    if pk:
        # crude timepoint counter: tokens like "0.5 h", "24 h", "Hour 12"
        times = re.findall(r"\b\d{1,3}(?:\.\d+)?\s*(?:h\b|hours?\b|hr\b)", pk.lower())
        if len(times) >= 6:
            add(QCFinding(id="sampling", category="PK & design",
                title="Sampling schedule looks adequate", status=QCStatus.PASS,
                detail=f"Detected {len(times)} time references in the PK sampling schedule.",
                section="study_assessments"))
        else:
            add(QCFinding(id="sampling", category="PK & design",
                title="Sparse PK sampling schedule", status=QCStatus.WARNING,
                detail=f"Only {len(times)} time references detected; a BE study typically needs "
                       f"12–18 samples to characterize Cmax and AUC.",
                section="study_assessments"))

    # ── 6. Dose vs label ──────────────────────────────────────────────────
    dose_strength = population_config.get("dose_strength")
    treatment = _txt(sections, "treatment")
    if dose_strength and treatment:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(mg|mcg|µg|g|iu|units?)", str(dose_strength).lower())
        if m:
            tok = f"{m.group(1)} {m.group(2)}"
            if m.group(1) in treatment.lower():
                add(QCFinding(id="dose_label", category="Regulatory",
                    title="Dose strength matches configuration", status=QCStatus.PASS,
                    detail=f"Treatment section references the configured strength ({tok}).",
                    section="treatment"))
            else:
                add(QCFinding(id="dose_label", category="Regulatory",
                    title="Dose strength not found in Treatment", status=QCStatus.WARNING,
                    detail=f"Configured strength '{dose_strength}' was not located in the Treatment text.",
                    section="treatment"))

    # ── 7. Regulatory references present ──────────────────────────────────
    refs = _txt(sections, "references")
    if refs:
        if _has_any(refs, "bioequivalence", "ICH", "FDA", "21 CFR", "EMA"):
            add(QCFinding(id="reg_refs", category="Regulatory",
                title="Regulatory references present", status=QCStatus.PASS,
                detail="References cite the expected regulatory guidance.",
                section="references"))
        else:
            add(QCFinding(id="reg_refs", category="Regulatory",
                title="No regulatory guidance in references", status=QCStatus.WARNING,
                detail="Expected citations to FDA/ICH/EMA guidance were not found.",
                section="references"))

    # ── 8. Gold-standard feature coverage ─────────────────────────────────
    # Verify every feature in the canonical manifest that APPLIES to this study
    # type is present. Missing content features become WARNINGs tied to their
    # section, which the three-stage pipeline's remediation pass then regenerates
    # — so every protocol converges to the same applicable feature set.
    content_feats = applicable_content_features(study_type)
    present_count = 0
    for feat in content_feats:
        if feat.present(sections):
            present_count += 1
            add(QCFinding(
                id=f"gs_{feat.id}", category="Gold Standard",
                title=feat.label, status=QCStatus.PASS,
                detail="Gold-standard feature present.", section=feat.section,
            ))
        else:
            add(QCFinding(
                id=f"gs_{feat.id}", category="Gold Standard",
                title=f"Missing gold-standard feature: {feat.label}",
                status=QCStatus.WARNING, detail=feat.rationale, section=feat.section,
            ))
    # Deterministic features are always injected by the export — report as PASS so
    # the gold-standard checklist is complete.
    for dfeat in applicable_deterministic_features(study_type):
        add(QCFinding(
            id=f"gs_{dfeat.id}", category="Gold Standard",
            title=dfeat.label, status=QCStatus.PASS, detail=dfeat.detail,
        ))
    # Headline compliance line.
    total = len(content_feats)
    add(QCFinding(
        id="gs_compliance", category="Gold Standard",
        title=f"Gold-standard compliance: {present_count}/{total} applicable content features present",
        status=QCStatus.PASS if present_count == total else QCStatus.WARNING,
        detail="All applicable gold-standard features are present."
        if present_count == total else
        "Some gold-standard features are missing; they will be regenerated automatically.",
    ))

    return report
