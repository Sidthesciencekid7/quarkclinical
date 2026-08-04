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
from typing import Any, Callable, Optional

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
# Engine-fact helpers
# ---------------------------------------------------------------------------
# The checks in the second half of this module compare GENERATED TEXT against a
# deterministic engine result that was computed once at creation time and stored
# under an underscore-prefixed population_config key (_sampling_schedule,
# _bioanalytical, _safety_gating, _ddi, _randomization). Protocols generated
# before an engine existed simply have no such key, so every helper and every
# check degrades to NOT_APPLICABLE / WARNING and never raises.

# The sampling engine owns the parsing + verification of its own schedule, so
# the QC layer calls it rather than re-implementing the tolerance rules. Guarded
# because a missing engine must downgrade the check, never break the report.
try:  # pragma: no cover - defensive import
    from services.sampling_schedule import (
        TERMINAL_MIN_HALF_LIVES as _TERMINAL_MIN_HALF_LIVES,
    )
    from services.sampling_schedule import (
        TERMINAL_TARGET_HALF_LIVES as _TERMINAL_TARGET_HALF_LIVES,
    )
    from services.sampling_schedule import verify_sampling_schedule as _verify_sampling
except Exception:  # noqa: BLE001
    _verify_sampling = None
    _TERMINAL_MIN_HALF_LIVES = 3.0
    _TERMINAL_TARGET_HALF_LIVES = 5.0


_Add = Callable[[QCFinding], None]

_QC_STATUS_BY_VALUE = {s.value: s for s in QCStatus}

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
}


def _status_from(value: Any, default: QCStatus = QCStatus.WARNING) -> QCStatus:
    """Map an engine's status string onto QCStatus; never raises on junk."""
    return _QC_STATUS_BY_VALUE.get(str(value or "").strip().lower(), default)


def _facts(population_config: dict[str, Any], key: str) -> dict[str, Any]:
    """Fetch a computed engine block. Always a dict, even when absent/corrupt."""
    v = population_config.get(key)
    return v if isinstance(v, dict) else {}


def _rows(block: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Fetch a list-of-dicts field from an engine block, defensively."""
    v = block.get(key)
    if not isinstance(v, list):
        return []
    return [r for r in v if isinstance(r, dict)]


def _f(value: Any) -> Optional[float]:
    """float() that returns None instead of raising, and rejects NaN/inf."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _joined(sections: dict[str, str], *keys: str) -> str:
    return "\n".join(_txt(sections, k) for k in keys).strip()


def _normalise_numbers(text: str) -> str:
    """Lowercase and strip thousands separators so '5,000' matches 5000."""
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", (text or "").lower())


def _states_number(text: str, n: Any, rel_tol: float = 0.02) -> bool:
    """True when `text` states the value `n`.

    Whole numbers must appear as a standalone digit run (or, up to twelve, as a
    word) so that 60 does not match inside 160. Fractional values are matched
    against every number in the text within a relative tolerance, because the
    model is allowed to write 0.5 as 0.50.
    """
    val = _f(n)
    if val is None or not text:
        return False
    low = _normalise_numbers(text)
    if float(val).is_integer():
        i = int(val)
        if re.search(r"(?<![\d.])%d(?![\d.])" % i, low):
            return True
        word = _NUMBER_WORDS.get(i)
        return bool(word and re.search(r"\b%s\b" % word, low))
    tol = max(abs(val) * rel_tol, 1e-9)
    for m in re.finditer(r"\d+(?:\.\d+)?", low):
        if abs(float(m.group(0)) - val) <= tol:
            return True
    return False


def _states_count(text: str, n: Any) -> bool:
    """True when `text` states `n` as a THRESHOLD, not merely as a stray digit.

    "2 or more subjects", "at least 2", ">= 2". A bare "2" anywhere in a safety
    section is almost guaranteed (Grade 2, 2 hours, Period 2), so a bare-digit
    match would make the halting-count check pass on every protocol - which is
    the exact false confidence this check exists to remove.
    """
    val = _f(n)
    if val is None or not text or not float(val).is_integer():
        return False
    i = int(val)
    alts = [str(i)]
    word = _NUMBER_WORDS.get(i)
    if word:
        alts.append(word)
    num = "(?:%s)" % "|".join(re.escape(a) for a in alts)
    low = _normalise_numbers(text)
    pats = (
        r"(?:at least|>=|minimum of|a minimum of|no fewer than)\s*%s\b" % num,
        r"\b%s\s*(?:\([a-z]+\)\s*)?or\s+more\b" % num,
        r"\b%s\s+(?:or\s+more\s+)?(?:subjects|volunteers|participants|patients|individuals)\b" % num,
        r"\b%s\s+or\s+greater\b" % num,
    )
    return any(re.search(p, low) for p in pats)


def _windows(text: str, keywords: tuple[str, ...], radius: int = 300) -> str:
    """Concatenate the neighbourhood of every keyword hit, lowercased.

    Scoping a numeric check to the sentences that actually discuss the topic is
    what turns "the digit 2 appears somewhere" into "2 is stated as the halting
    threshold". Returns "" when no keyword occurs at all, which the callers
    treat as 'the topic is missing' rather than 'the number is missing'.
    """
    if not text:
        return ""
    low = text.lower()
    spans: list[list[int]] = []
    for kw in keywords:
        for m in re.finditer(re.escape(kw.lower()), low):
            spans.append([max(0, m.start() - radius), min(len(low), m.end() + radius)])
    if not spans:
        return ""
    spans.sort()
    merged = [spans[0]]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return " ... ".join(low[s:e] for s, e in merged)


_HTML_TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)


def _table_blocks(text: str) -> list[str]:
    """Every HTML <table> and every contiguous Markdown pipe-table in `text`."""
    if not text:
        return []
    blocks: list[str] = []
    for m in _HTML_TABLE_RE.finditer(text):
        if len(re.findall(r"<tr\b", m.group(0), re.IGNORECASE)) >= 2:
            blocks.append(m.group(0))
    run: list[str] = []
    for line in text.splitlines():
        if line.count("|") >= 2:
            run.append(line)
        else:
            if len(run) >= 3:
                blocks.append("\n".join(run))
            run = []
    if len(run) >= 3:
        blocks.append("\n".join(run))
    return blocks


def _sentence_around(text: str, start: int, end: int, radius: int = 130) -> str:
    return text[max(0, start - radius):min(len(text), end + radius)].lower()


_NEGATED_RE = re.compile(
    r"\b(?:is not|are not|was not|were not|does not|do not|no evidence|"
    r"not a substrate|not metabol|not known to be|neither)\b"
)


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

    # ── 5. PK sampling schedule vs the locked schedule ────────────────────
    _check_sampling(add, sections, population_config)

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

    # ── 9-13. Generated text vs the deterministic engine facts ────────────
    # Each of these degrades to NOT_APPLICABLE when its engine block is absent
    # (protocols created before the engine existed) and never raises.
    _check_cv_provenance(add, sections, ss)
    _check_bioanalytical(add, sections, population_config)
    _check_safety_gating(add, sections, population_config)
    _check_ddi(add, sections, population_config)
    _check_randomization(add, sections, population_config)

    return report


# ---------------------------------------------------------------------------
# Engine-fact checks
# ---------------------------------------------------------------------------


def _engine_absent(add: _Add, *, id: str, category: str, title: str, engine: str,
                   section: Optional[str] = None) -> None:
    """Uniform NOT_APPLICABLE line for a protocol predating an engine.

    Reported rather than silently skipped: a reviewer is entitled to know that a
    check did not run, and NOT_APPLICABLE never blocks submission_ready nor
    triggers the pipeline's regeneration pass.
    """
    add(QCFinding(
        id=id, category=category, title=title, status=QCStatus.NA,
        detail=(f"No computed {engine} facts are stored on this protocol, so the "
                f"generated text could not be verified against them. Protocols "
                f"created before this engine was added will not carry them; "
                f"regenerate the protocol to enable the check."),
        section=section,
    ))


# --- 5. PK sampling schedule -----------------------------------------------

# The legacy heuristic, kept ONLY as the fallback for protocols with no computed
# schedule. It counts hour-shaped tokens and cannot tell a schedule that brackets
# Tmax from one that steps straight over it, which is why the real check below
# replaced it.
_LEGACY_TIME_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?\s*(?:h\b|hours?\b|hr\b)")
_LEGACY_MIN_TIME_REFS = 6


def _check_sampling(add: _Add, sections: dict[str, str],
                    population_config: dict[str, Any]) -> None:
    """Verify the STATED sampling times against the computed schedule.

    Three things are checked, in the order a lost study fails on them:
      1. peak bracketing - a peak that is not bracketed clips Cmax, in both
         arms, by an amount that does not cancel, and no re-analysis recovers it;
      2. terminal reach - a tail short of the locked horizon blows the planned
         AUC extrapolation (AUC(0-t) must stay >= 80% of AUC(0-inf));
      3. AUC %extrap - the protocol must state the acceptance limit it promises.
    """
    pk = _txt(sections, "study_assessments")
    sched = _facts(population_config, "_sampling_schedule")
    timepoints = _rows(sched, "timepoints")

    if not sched or not timepoints:
        # Legacy behaviour, unchanged, for protocols with no computed schedule.
        if pk:
            times = _LEGACY_TIME_RE.findall(pk.lower())
            if len(times) >= _LEGACY_MIN_TIME_REFS:
                add(QCFinding(id="sampling", category="PK & design",
                    title="Sampling schedule looks adequate", status=QCStatus.PASS,
                    detail=f"Detected {len(times)} time references in the PK sampling "
                           f"schedule. No computed schedule is stored on this protocol, "
                           f"so only the count could be checked.",
                    section="study_assessments"))
            else:
                add(QCFinding(id="sampling", category="PK & design",
                    title="Sparse PK sampling schedule", status=QCStatus.WARNING,
                    detail=f"Only {len(times)} time references detected; a BE study "
                           f"typically needs 12-18 samples to characterize Cmax and AUC.",
                    section="study_assessments"))
        return

    if _verify_sampling is None:  # pragma: no cover - defensive
        add(QCFinding(id="sampling", category="PK & design",
            title="Sampling schedule could not be verified", status=QCStatus.WARNING,
            detail="The sampling-schedule engine could not be loaded, so the stated "
                   "timepoints were not compared against the locked schedule.",
            section="study_assessments"))
        return

    try:
        out = _verify_sampling(pk, sched)
    except Exception as exc:  # noqa: BLE001 - a QC check must never break a report
        add(QCFinding(id="sampling", category="PK & design",
            title="Sampling schedule check did not complete", status=QCStatus.WARNING,
            detail=f"The stated timepoints could not be compared against the locked "
                   f"schedule ({type(exc).__name__}). Verify the schedule by hand.",
            section="study_assessments"))
        return

    n_expected = len(timepoints)
    n_matched = len(out.get("matched") or [])
    status = _status_from(out.get("status"))
    title = {
        QCStatus.PASS: "Sampling schedule matches the locked schedule",
        QCStatus.FAIL: "Sampling schedule contradicts the locked schedule",
        QCStatus.NA: "No locked sampling schedule to check against",
    }.get(status, "Sampling schedule only partly matches the locked schedule")
    add(QCFinding(id="sampling", category="PK & design", title=title, status=status,
        detail=str(out.get("detail") or "")
               + f" ({n_matched}/{n_expected} locked timepoints matched.)",
        section="study_assessments"))

    # --- peak bracketing, reported separately because it is irreversible ---
    pb = sched.get("peak_bracketing") if isinstance(sched.get("peak_bracketing"), dict) else {}
    tmax = _f(pb.get("tmax_hours"))
    before = out.get("points_before_tmax")
    after = out.get("points_after_tmax")
    if isinstance(before, int) and isinstance(after, int) and out.get("reported"):
        if before >= 2 and after >= 2:
            add(QCFinding(id="sampling_peak", category="PK & design",
                title="Cmax is bracketed by the stated schedule", status=QCStatus.PASS,
                detail=f"The stated times place {before} sample(s) before and {after} "
                       f"after the literature Tmax of {tmax if tmax is not None else '?'} h, "
                       f"so the observed Cmax is not truncated by the schedule.",
                section="study_assessments"))
        else:
            add(QCFinding(id="sampling_peak", category="PK & design",
                title="Cmax can be clipped by the stated schedule", status=QCStatus.FAIL,
                detail=f"Only {before} sample(s) before and {after} after the Tmax of "
                       f"{tmax if tmax is not None else '?'} h appear in the generated "
                       f"section; at least two on each side are required. A clipped Cmax "
                       f"biases both arms and cannot be recovered after dosing - the "
                       f"blood was never drawn.",
                section="study_assessments"))

    # --- terminal reach, in half-lives -------------------------------------
    t_half = _f(sched.get("t_half_hours"))
    rep_last = _f(out.get("reported_last_hours"))
    exp_last = _f(out.get("expected_last_hours")) or _f(sched.get("last_timepoint_hours"))
    if t_half and t_half > 0 and rep_last is not None:
        hl = rep_last / t_half
        locked_hl = _f(sched.get("last_timepoint_half_lives"))
        locked_txt = (f" The locked schedule runs to {exp_last:g} h "
                      f"({locked_hl:g} half-lives)." if exp_last and locked_hl else "")
        if hl >= _TERMINAL_TARGET_HALF_LIVES:
            add(QCFinding(id="sampling_terminal", category="PK & design",
                title="Terminal phase adequately sampled", status=QCStatus.PASS,
                detail=f"The last stated sample at {rep_last:g} h is {hl:.1f} terminal "
                       f"half-lives after dosing (target "
                       f"{_TERMINAL_TARGET_HALF_LIVES:g}).{locked_txt}",
                section="study_assessments"))
        elif hl >= _TERMINAL_MIN_HALF_LIVES:
            add(QCFinding(id="sampling_terminal", category="PK & design",
                title="Terminal sampling shorter than the locked horizon",
                status=QCStatus.WARNING,
                detail=f"The last stated sample at {rep_last:g} h is only {hl:.1f} "
                       f"half-lives after dosing, below the {_TERMINAL_TARGET_HALF_LIVES:g} "
                       f"the schedule was built to reach.{locked_txt} The extrapolated "
                       f"share of AUC will be larger than the protocol claims.",
                section="study_assessments"))
        else:
            add(QCFinding(id="sampling_terminal", category="PK & design",
                title="Terminal sampling too short - AUC extrapolation fails",
                status=QCStatus.FAIL,
                detail=f"The last stated sample at {rep_last:g} h is only {hl:.1f} "
                       f"terminal half-lives after dosing; below "
                       f"{_TERMINAL_MIN_HALF_LIVES:g} half-lives AUC(0-t) cannot reach "
                       f"80% of AUC(0-inf) by construction.{locked_txt}",
                section="study_assessments"))

    # --- the AUC extrapolation limit must be written down ------------------
    extrap = _f(sched.get("auc_extrap_pct"))
    limit = _f(sched.get("auc_extrap_limit_pct"))
    if limit is not None and sched.get("auc_metric") and extrap is not None:
        scan = _joined(sections, "study_assessments", "statistical_analysis_plan")
        win = _windows(scan, ("extrapolat", "auc0-inf", "auc(0-inf)", "auc0-t",
                              "auc(0-t)", "residual area"), radius=260)
        if not win:
            add(QCFinding(id="sampling_auc_extrap", category="PK & design",
                title="AUC extrapolation criterion not stated", status=QCStatus.WARNING,
                detail=f"The locked schedule predicts {extrap:g}% of AUC(0-inf) will be "
                       f"extrapolated against a {limit:g}% limit, but neither the "
                       f"assessments nor the SAP text states the extrapolation "
                       f"acceptance criterion.",
                section="statistical_analysis_plan"))
        elif not _states_number(win, limit):
            add(QCFinding(id="sampling_auc_extrap", category="PK & design",
                title="AUC extrapolation limit not stated", status=QCStatus.WARNING,
                detail=f"AUC extrapolation is discussed but the {limit:g}% limit "
                       f"(AUC(0-t) at least 80% of AUC(0-inf)) is not stated. The "
                       f"schedule was built to achieve {extrap:g}%.",
                section="statistical_analysis_plan"))
        else:
            add(QCFinding(id="sampling_auc_extrap", category="PK & design",
                title="AUC extrapolation criterion stated", status=QCStatus.PASS,
                detail=f"The {limit:g}% extrapolation limit is stated; the locked "
                       f"schedule predicts {extrap:g}%.",
                section="statistical_analysis_plan"))

    # --- flags the engine says the protocol must surface -------------------
    scan_all = _joined(sections, "study_assessments", "safety_monitoring",
                       "study_design", "statistical_analysis_plan")
    for flag in _rows(sched, "flags"):
        if not flag.get("must_surface_in_protocol"):
            continue
        critical = str(flag.get("severity") or "").lower() == "critical"
        add(QCFinding(
            id="sampling_flag_%s" % (flag.get("key") or "unnamed"),
            category="PK & design",
            title="Sampling constraint not disclosed: %s" % (flag.get("key") or "unnamed"),
            status=QCStatus.FAIL if critical else QCStatus.WARNING,
            detail=str(flag.get("message") or "")
                   + " The engine marked this as must-surface-in-protocol; confirm the "
                     "generated text discloses it.",
            section="study_assessments",
        ))
    if sched.get("must_disclose") and not scan_all:
        add(QCFinding(id="sampling_disclosure", category="PK & design",
            title="Sampling disclosures cannot be checked", status=QCStatus.WARNING,
            detail="The schedule carries disclosures that must appear in the protocol, "
                   "but the relevant sections are empty.",
            section="study_assessments"))


# --- 9. CV provenance honesty ----------------------------------------------

_CV_TOKEN_RE = re.compile(r"\bcv\b|\bcv%|coefficient of variation", re.IGNORECASE)
_CV_EVIDENCE_TOKEN_RE = re.compile(
    r"\bpmid\b|\bsource\b|\bstudy\b|\bstudies\b|\breference\b|\btrial\b|"
    r"\bpublicat|\bpubmed\b|\bcitation\b",
    re.IGNORECASE,
)
_LIT_ATTRIBUTION_RE = re.compile(
    r"published|literature|peer[- ]reviewed|\bpmid\b|pubmed|reported in",
    re.IGNORECASE,
)
_INVESTIGATOR_CV_RE = re.compile(
    r"overrid|manual|investigator|sponsor|\buser\b|hand[- ]?entered|"
    r"\bentered\b|\bsupplied\b|\binput\b",
    re.IGNORECASE,
)
_CV_DISCLOSED_RE = re.compile(
    r"overrid|investigator[- ]specified|sponsor[- ]specified|manually|"
    r"specified by the investigator|not (?:taken )?from the literature",
    re.IGNORECASE,
)


def _cv_provenance(cv_source: Any) -> str:
    """Classify where the powering CV came from: literature / investigator / assumed.

    The stored string is free-form ("literature (highest observed)", "override",
    "not specified"), so this normalises rather than comparing for equality.
    """
    s = str(cv_source or "").strip().lower()
    if s in ("", "not specified", "unspecified", "unknown", "none", "n/a"):
        return "unknown"
    if "assum" in s or "default" in s:
        return "assumed"
    if _INVESTIGATOR_CV_RE.search(s):
        return "investigator"
    if "literature" in s or "publish" in s or "sourced" in s or "pmid" in s:
        return "literature"
    return "unknown"


def _cv_literature_tables(text: str) -> list[str]:
    """Tables that present published trials as the EVIDENCE for the CV.

    Requires both a CV column and a source/study/PMID column, so a summary-
    statistics shell that merely lists CV% as an output statistic is not
    mistaken for an attribution to literature.
    """
    return [
        b for b in _table_blocks(text)
        if _CV_TOKEN_RE.search(b) and _CV_EVIDENCE_TOKEN_RE.search(b)
    ]


def _check_cv_provenance(add: _Add, sections: dict[str, str],
                         ss: dict[str, Any]) -> None:
    """The powering CV must be attributed to where it ACTUALLY came from.

    A protocol that hand-enters a within-subject CV and then prints a table of
    published trials underneath it as the justification is telling a regulator
    that a number came from evidence it did not come from. That is a
    documentation-integrity defect, not a formatting one, and it is the failure
    mode that caused the manual CV override to be removed in the first place.
    The mirror image is checked too: a literature-derived CV with no supporting
    table is an unevidenced claim.
    """
    if not ss or not isinstance(ss, dict):
        return  # section 2 already reports the missing _sample_size block
    sap = _txt(sections, "statistical_analysis_plan")
    cv = _f(ss.get("cv_pct"))
    provenance = _cv_provenance(ss.get("cv_source"))
    raw_source = str(ss.get("cv_source") or "unrecorded")
    cv_txt = f"{cv:.1f}%" if cv is not None else "the powering CV"
    engine_rows = ss.get("cv_table")
    n_engine_rows = len(engine_rows) if isinstance(engine_rows, list) else 0

    if not sap:
        add(QCFinding(id="cv_provenance", category="Statistics",
            title="CV provenance cannot be checked", status=QCStatus.WARNING,
            detail=f"The Statistical Analysis Plan is empty, so the stated basis for "
                   f"{cv_txt} (recorded source: {raw_source}) could not be verified.",
            section="statistical_analysis_plan"))
        return

    tables = _cv_literature_tables(sap)
    cv_window = _windows(sap, ("within-subject cv", "intra-subject cv",
                               "coefficient of variation",
                               "sample size determination"), radius=420)
    prose_attribution = bool(_LIT_ATTRIBUTION_RE.search(cv_window)) if cv_window else False

    if provenance == "investigator":
        if tables:
            add(QCFinding(id="cv_provenance", category="Statistics",
                title="Investigator-specified CV presented as literature-derived",
                status=QCStatus.FAIL,
                detail=f"The sample size was powered on {cv_txt}, recorded as "
                       f"investigator-specified (cv_source='{raw_source}'), but the SAP "
                       f"presents a table of supporting published trials as its "
                       f"justification. The protocol therefore attributes a hand-entered "
                       f"number to literature it did not come from. Either remove the "
                       f"supporting-trials table and state plainly that the CV was "
                       f"specified by the investigator (with the justification on file), "
                       f"or power the study on the literature value the table actually "
                       f"supports.",
                section="statistical_analysis_plan"))
        elif prose_attribution:
            add(QCFinding(id="cv_provenance", category="Statistics",
                title="Investigator-specified CV attributed to the literature in prose",
                status=QCStatus.WARNING,
                detail=f"{cv_txt} is recorded as investigator-specified "
                       f"(cv_source='{raw_source}'), but the sample-size narrative "
                       f"credits published/literature sources for it. State the real "
                       f"provenance and keep the justification on file.",
                section="statistical_analysis_plan"))
        elif not _CV_DISCLOSED_RE.search(sap):
            add(QCFinding(id="cv_provenance", category="Statistics",
                title="Investigator-specified CV not disclosed as such",
                status=QCStatus.WARNING,
                detail=f"{cv_txt} was supplied rather than retrieved "
                       f"(cv_source='{raw_source}'), and the SAP does not say so. A "
                       f"reviewer will ask where the number came from; state it "
                       f"explicitly.",
                section="statistical_analysis_plan"))
        else:
            add(QCFinding(id="cv_provenance", category="Statistics",
                title="Investigator-specified CV correctly disclosed", status=QCStatus.PASS,
                detail=f"{cv_txt} is declared as investigator-specified and is not "
                       f"presented as a literature finding.",
                section="statistical_analysis_plan"))

    elif provenance == "assumed":
        if tables:
            add(QCFinding(id="cv_provenance", category="Statistics",
                title="Assumed CV presented as literature-derived", status=QCStatus.FAIL,
                detail=f"{cv_txt} is a default assumption (cv_source='{raw_source}') "
                       f"because no published within-subject CV could be retrieved, yet "
                       f"the SAP prints a table of supporting trials as its basis. Remove "
                       f"the table: it evidences a number the study was not powered on.",
                section="statistical_analysis_plan"))
        else:
            add(QCFinding(id="cv_provenance", category="Statistics",
                title="Assumed CV declared without a literature table",
                status=QCStatus.PASS,
                detail=f"{cv_txt} is disclosed as an assumption and no supporting-trials "
                       f"table is presented for it.",
                section="statistical_analysis_plan"))

    elif provenance == "literature":
        if tables:
            add(QCFinding(id="cv_provenance", category="Statistics",
                title="Literature CV supported by a trials table", status=QCStatus.PASS,
                detail=f"{cv_txt} is literature-derived (cv_source='{raw_source}') and "
                       f"the SAP presents the supporting trials.",
                section="statistical_analysis_plan"))
        else:
            rows_txt = (f", {n_engine_rows} extracted trial row(s) available"
                        if n_engine_rows else "")
            add(QCFinding(id="cv_provenance", category="Statistics",
                title="Literature CV stated without its supporting trials",
                status=QCStatus.WARNING,
                detail=f"{cv_txt} is recorded as literature-derived "
                       f"(cv_source='{raw_source}'{rows_txt}), but the SAP contains no "
                       f"table identifying the studies, populations and parameters it "
                       f"came from. An unevidenced CV is the first thing a reviewer "
                       f"challenges.",
                section="statistical_analysis_plan"))

    else:
        tail = (" The SAP nonetheless presents a supporting-trials table."
                if tables else " State the basis explicitly in the SAP.")
        add(QCFinding(id="cv_provenance", category="Statistics",
            title="CV provenance not recorded", status=QCStatus.WARNING,
            detail=f"The stored cv_source is '{raw_source}', which does not identify "
                   f"whether {cv_txt} was retrieved from the literature, assumed, or "
                   f"supplied by the investigator." + tail,
            section="statistical_analysis_plan"))


# --- 10. Bioanalytical: calibration range and ISR ---------------------------

# Concentration units the generated text may use, with their factor to ng/mL.
# The micro sign is built from its codepoint rather than typed literally, so every
# line of this block stays ASCII (this repo has been bitten by cp1252 before).
_MICROGRAM = chr(0xB5) + "g"   # MICRO SIGN + g, i.e. "ug" written with the symbol
_CONC_UNIT_NG = {
    "pg": 0.001, "ng": 1.0, "ug": 1000.0, "mcg": 1000.0, _MICROGRAM: 1000.0,
    "mg": 1000000.0,
}
_CONC_UNIT_ALT = "|".join(("pg", "ng", "ug", "mcg", _MICROGRAM, "mg"))

_LLOQ_LABEL = (r"lloq|lower limit of quantitation|lower limit of quantification|"
               r"limit of quantitation|limit of quantification")
_ULOQ_LABEL = (r"uloq|upper limit of quantitation|upper limit of quantification")


def _parse_conc_ng(text: str, label_pat: str) -> list[float]:
    """EVERY concentration (normalised to ng/mL) stated near `label_pat`.

    All occurrences, not just the first: a protocol that states the correct
    range in one paragraph and a different one three paragraphs later is exactly
    the kind of defect this check exists for, and stopping at the first match
    would report it as clean.
    """
    if not text:
        return []
    pat = (r"(?:%s)[^0-9]{0,60}?(\d+(?:[.,]\d+)?)\s*(%s)\s*/\s*m\s*l"
           % (label_pat, _CONC_UNIT_ALT))
    out: list[float] = []
    for m in re.finditer(pat, text, re.IGNORECASE):
        val = _f(m.group(1).replace(",", ""))
        if val is not None:
            out.append(val * _CONC_UNIT_NG.get(m.group(2).lower(), 1.0))
    return out


def _check_bioanalytical(add: _Add, sections: dict[str, str],
                         population_config: dict[str, Any]) -> None:
    """The calibration range must be stated AND must actually bracket exposure.

    A ULOQ below the anticipated Cmax means every peak sample needs dilution or
    is reported as ">ULOQ" - the study loses Cmax, the primary endpoint. An LLOQ
    above 5% of Cmax truncates the terminal phase and takes lambda_z, AUC(0-inf)
    and t1/2 with it. Both are stated as numbers in the protocol, so both can be
    checked deterministically against the engine's derivation.
    """
    bio = _facts(population_config, "_bioanalytical")
    if not bio:
        _engine_absent(add, id="bioanalytical_facts", category="Bioanalytical",
                       title="Bioanalytical plan not verified",
                       engine="bioanalytical", section="study_assessments")
        return

    text = _joined(sections, "study_assessments", "statistical_analysis_plan")
    if not text:
        add(QCFinding(id="bioanalytical_range", category="Bioanalytical",
            title="Bioanalytical text missing", status=QCStatus.WARNING,
            detail="Study Assessments is empty, so the calibration range, the assay "
                   "acceptance criteria and the ISR requirement could not be checked.",
            section="study_assessments"))
        return

    cal = bio.get("calibration") if isinstance(bio.get("calibration"), dict) else {}
    derived = bool(cal.get("derived"))
    lloq = _f(cal.get("lloq"))
    uloq = _f(cal.get("uloq"))
    acmax = cal.get("anticipated_cmax")
    cmax = _f(acmax.get("value")) if isinstance(acmax, dict) else None

    stated_lloq = _parse_conc_ng(text, _LLOQ_LABEL)
    stated_uloq = _parse_conc_ng(text, _ULOQ_LABEL)

    # The calibration range is the bioanalytical laboratory's to set during
    # method development and validation; it belongs in the BMV report, not the
    # clinical protocol. So the check is now inverted: ANY numeric LLOQ/ULOQ in
    # the protocol text is the defect, whether or not the engine derived one.
    #
    # This also removes a check that fired wrongly in practice. For a biologic
    # with a ug/mL Cmax the 5%-of-Cmax anchor produced an LLOQ above the Cmax
    # itself, and the old "LLOQ too high" FAIL was reporting a number the app
    # had generated against a rule the app had also generated - noise, not a
    # finding about the protocol.
    if stated_lloq or stated_uloq:
        add(QCFinding(id="bioanalytical_range", category="Bioanalytical",
            title="Protocol states a numeric calibration range",
            status=QCStatus.FAIL,
            detail=f"The text states a numeric assay range (LLOQ "
                   f"{stated_lloq or 'not stated'}, ULOQ {stated_uloq or 'not stated'} "
                   f"ng/mL). The LLOQ, ULOQ and QC concentrations are established by the "
                   f"analytical laboratory during method validation and are recorded in "
                   f"the Bioanalytical Method Validation report, which is approved "
                   f"separately from this protocol. Committing the study to a specific "
                   f"concentration here creates a protocol deviation the moment the "
                   f"validated method differs. State only that a validated method will "
                   f"be used and that its range is documented in the BMV report.",
            section="study_assessments"))
    else:
        add(QCFinding(id="bioanalytical_range", category="Bioanalytical",
            title="Calibration range correctly left to method validation",
            status=QCStatus.PASS,
            detail="No numeric LLOQ or ULOQ is asserted in the protocol. The range is "
                   "the analytical laboratory's to establish and is documented in the "
                   "Bioanalytical Method Validation report.",
            section="study_assessments"))

    # Consistency of whatever range the text DOES state against the exposure.
    if cmax is not None and cmax > 0:
        contradictions = []
        if len({round(v, 6) for v in stated_lloq}) > 1:
            contradictions.append(
                "LLOQ: " + ", ".join(f"{v:g}" for v in sorted(set(stated_lloq))))
        if len({round(v, 6) for v in stated_uloq}) > 1:
            contradictions.append(
                "ULOQ: " + ", ".join(f"{v:g}" for v in sorted(set(stated_uloq))))
        if contradictions:
            add(QCFinding(id="bioanalytical_range_conflict", category="Bioanalytical",
                title="Protocol states more than one calibration limit",
                status=QCStatus.FAIL,
                detail="The generated text states conflicting assay limits ("
                       + "; ".join(contradictions) + " ng/mL). Only one validated range "
                       "can apply, and the laboratory cannot be given two.",
                section="study_assessments"))

    # Incurred sample reanalysis - the only check that the method reproduces a
    # result in real (rather than spiked) samples.
    isr = bio.get("isr") if isinstance(bio.get("isr"), dict) else {}
    if isr.get("required"):
        if re.search(r"incurred sample re-?analys|incurred sample re-?assay|\bisr\b",
                     text, re.IGNORECASE):
            add(QCFinding(id="bioanalytical_isr", category="Bioanalytical",
                title="Incurred sample reanalysis specified", status=QCStatus.PASS,
                detail="ISR is required by the bioanalytical plan and is named in the "
                       "generated text.",
                section="study_assessments"))
        else:
            add(QCFinding(id="bioanalytical_isr", category="Bioanalytical",
                title="Incurred sample reanalysis not specified", status=QCStatus.WARNING,
                detail=f"ISR is mandatory (FDA BMV 2018 / ICH M10) but the generated text "
                       f"does not mention it. Required rule: "
                       f"{isr.get('rule') or 'per ICH M10 section 7.3'}",
                section="study_assessments"))

    # Engine flags: high severity is a gap, medium is a disclosed assumption.
    flags = _rows(bio, "flags")
    high = [f for f in flags if str(f.get("severity") or "").lower() == "high"]
    medium = [f for f in flags if str(f.get("severity") or "").lower() == "medium"]
    if high:
        add(QCFinding(id="bioanalytical_flags_high", category="Bioanalytical",
            title=f"{len(high)} bioanalytical gap(s) flagged by the engine",
            status=QCStatus.WARNING,
            detail=" | ".join(str(f.get("message") or f.get("key") or "") for f in high),
            section="study_assessments"))
    if medium:
        add(QCFinding(id="bioanalytical_flags_medium", category="Bioanalytical",
            title=f"{len(medium)} bioanalytical assumption(s) requiring sign-off",
            status=QCStatus.ASSUMPTION,
            detail=" | ".join(
                str(f.get("message") or f.get("key") or "") for f in medium),
            section="study_assessments"))


# --- 11. Safety gating: quantitative stopping rules -------------------------

_HALT_KEYWORDS = ("halt", "suspend", "stop dosing", "cease dosing", "pause dosing",
                  "further dosing", "no further subjects", "stopping rule",
                  "halting rule")
_QT_KEYWORDS = ("qtc", "qt interval", "qtcf", "corrected qt")
_HEPATIC_KEYWORDS = ("uln", "hy's law", "hys law", "bilirubin", "aminotransferase")
_DISCHARGE_KEYWORDS = ("discharge", "released from the unit", "release from the unit",
                       "leave the unit", "confinement")

_MEDIC_RE = r"(?:physician|investigator|medically[- ]qualified|medical officer)"
_AUTHORISE_RE = r"(?:authoris|authoriz|approv|sign(?:ed|ature)?\b|permission)"


def _authorised_discharge(text: str) -> bool:
    """True when discharge is tied to a named medic's authorisation.

    Both word orders occur naturally ("the Investigator authorises discharge" /
    "authorisation to discharge by the Principal Investigator"), so requiring one
    of them would fail a correct protocol.
    """
    if not text:
        return False
    return bool(
        re.search(r"%s[^.]{0,140}%s" % (_MEDIC_RE, _AUTHORISE_RE), text, re.IGNORECASE)
        or re.search(r"%s[^.]{0,140}%s" % (_AUTHORISE_RE, _MEDIC_RE), text, re.IGNORECASE)
    )


def _check_safety_gating(add: _Add, sections: dict[str, str],
                         population_config: dict[str, Any]) -> None:
    """Stopping rules must carry their NUMBERS, not an allusion to them.

    "Dosing will be halted if several subjects experience a serious event" is
    not a stopping rule - nobody can apply it at 2 a.m. The engine computes the
    exact counts and boundaries from the enrolled N and the label; this check
    proves each one survived into the narrative.
    """
    gating = _facts(population_config, "_safety_gating")
    if not gating:
        _engine_absent(add, id="safety_gating_facts", category="Safety",
                       title="Safety stopping rules not verified",
                       engine="safety gating", section="safety_monitoring")
        return

    safety = _txt(sections, "safety_monitoring")
    counts = gating.get("counts") if isinstance(gating.get("counts"), dict) else {}
    thresholds = gating.get("thresholds") if isinstance(gating.get("thresholds"), dict) else {}

    if not safety:
        add(QCFinding(id="safety_halting", category="Safety",
            title="Safety Monitoring section empty", status=QCStatus.FAIL,
            detail="The locked halting rules and stopping boundaries have nowhere to "
                   "live: the Safety Monitoring section contains no text.",
            section="safety_monitoring"))
        return

    # (a) cohort halting count - the single highest-value line in this check.
    halt_win = _windows(safety, _HALT_KEYWORDS, radius=340)
    k_g3 = counts.get("related_grade3_halt")
    if k_g3 is None:
        pass
    elif not halt_win:
        add(QCFinding(id="safety_halting", category="Safety",
            title="No cohort halting rule found", status=QCStatus.FAIL,
            detail=f"The engine locked a halt at {k_g3} or more subjects with a related "
                   f"Grade 3 or higher event, but the Safety Monitoring text contains no "
                   f"halting/stopping language at all.",
            section="safety_monitoring"))
    elif _states_count(halt_win, k_g3):
        add(QCFinding(id="safety_halting", category="Safety",
            title="Cohort halting count stated", status=QCStatus.PASS,
            detail=f"The locked threshold of {k_g3} or more subjects is stated in the "
                   f"halting rules.",
            section="safety_monitoring"))
    else:
        add(QCFinding(id="safety_halting", category="Safety",
            title="Halting rule stated without its threshold", status=QCStatus.FAIL,
            detail=f"The Safety Monitoring text describes halting dosing but never "
                   f"states the locked threshold of {k_g3} or more subjects with a "
                   f"related Grade 3 or higher event. A rule the site cannot count "
                   f"against is not a stopping rule.",
            section="safety_monitoring"))

    # The other three locked counts are reported together to keep the report short.
    other = [
        ("QT boundary", counts.get("qt_boundary_subjects_halt")),
        ("same related Grade >= 2 event cluster", counts.get("same_event_grade2_cluster_halt")),
        ("AE-related withdrawals", counts.get("ae_withdrawal_halt")),
    ]
    missing_counts = [
        name for name, val in other
        if val is not None and halt_win and not _states_count(halt_win, val)
    ]
    if missing_counts and halt_win:
        add(QCFinding(id="safety_halting_counts", category="Safety",
            title="Secondary halting thresholds not stated", status=QCStatus.WARNING,
            detail="These locked halting counts do not appear as thresholds in the "
                   "halting rules: "
                   + "; ".join(
                       f"{name} ({val})" for name, val in other
                       if name in missing_counts
                   ) + ".",
            section="safety_monitoring"))

    # (b) QT stopping boundary - ICH E14 numbers a regulator checks first.
    qt = thresholds.get("qt") if isinstance(thresholds.get("qt"), dict) else {}
    qt_abs = qt.get("qtcf_absolute_ms")
    qt_delta = qt.get("qtcf_delta_from_baseline_ms")
    if qt_abs is not None or qt_delta is not None:
        qt_win = _windows(safety, _QT_KEYWORDS, radius=340)
        if not qt_win:
            add(QCFinding(id="safety_qt", category="Safety",
                title="No QT stopping boundary in the protocol", status=QCStatus.FAIL,
                detail=f"The locked QT boundary (QTcF above {qt_abs} ms, or a rise of "
                       f"{qt_delta} ms from baseline) does not appear: the Safety "
                       f"Monitoring text never mentions QT or QTcF.",
                section="safety_monitoring"))
        elif _states_number(qt_win, qt_abs) and _states_number(qt_win, qt_delta):
            add(QCFinding(id="safety_qt", category="Safety",
                title="QT stopping boundary stated", status=QCStatus.PASS,
                detail=f"Both locked QT limits are stated: QTcF above {qt_abs} ms and a "
                       f"rise of {qt_delta} ms from baseline.",
                section="safety_monitoring"))
        else:
            add(QCFinding(id="safety_qt", category="Safety",
                title="QT stopping boundary incomplete", status=QCStatus.FAIL,
                detail=f"The QT discussion does not state both locked limits "
                       f"(absolute QTcF {qt_abs} ms"
                       + ("" if _states_number(qt_win, qt_abs) else " - MISSING")
                       + f"; change from baseline {qt_delta} ms"
                       + ("" if _states_number(qt_win, qt_delta) else " - MISSING")
                       + "). Both are required; the absolute limit alone misses a "
                         "subject who starts low and rises steeply.",
                section="safety_monitoring"))

    # (c) Hy's Law - hepatic discontinuation criterion.
    hys = thresholds.get("hys_law") if isinstance(thresholds.get("hys_law"), dict) else {}
    alt_x = hys.get("alt_or_ast_uln_multiple")
    bili_x = hys.get("total_bilirubin_uln_multiple")
    if alt_x is not None or bili_x is not None:
        hep_win = _windows(safety, _HEPATIC_KEYWORDS, radius=300)
        if "uln" not in safety.lower():
            add(QCFinding(id="safety_hepatic", category="Safety",
                title="Hepatic (Hy's Law) criterion absent", status=QCStatus.FAIL,
                detail=f"The locked hepatic rule ({alt_x} x ULN ALT or AST with total "
                       f"bilirubin above {bili_x} x ULN, other causes excluded) does not "
                       f"appear: the text never expresses a liver-test limit relative to "
                       f"the upper limit of normal.",
                section="safety_monitoring"))
        elif _states_number(hep_win, alt_x) and _states_number(hep_win, bili_x):
            add(QCFinding(id="safety_hepatic", category="Safety",
                title="Hy's Law criterion stated", status=QCStatus.PASS,
                detail=f"The {alt_x} x ULN aminotransferase and {bili_x} x ULN bilirubin "
                       f"multiples are both stated.",
                section="safety_monitoring"))
        else:
            add(QCFinding(id="safety_hepatic", category="Safety",
                title="Hy's Law multiples not both stated", status=QCStatus.WARNING,
                detail=f"ULN-relative liver limits are discussed but the locked "
                       f"multiples ({alt_x} x ULN ALT/AST and {bili_x} x ULN total "
                       f"bilirubin) were not both found alongside them.",
                section="safety_monitoring"))

    # (d) Discharge criteria, including the mandatory physician authorisation.
    disc = (gating.get("discharge_criteria")
            if isinstance(gating.get("discharge_criteria"), dict) else {})
    criteria = _rows(disc, "criteria")
    if criteria:
        disc_text = _joined(sections, "study_design", "study_assessments",
                            "safety_monitoring")
        disc_win = _windows(disc_text, _DISCHARGE_KEYWORDS, radius=420)
        mandatory = [c for c in criteria if c.get("mandatory")]
        if not disc_win:
            add(QCFinding(id="safety_discharge", category="Safety",
                title="Discharge criteria absent", status=QCStatus.WARNING,
                detail=f"The engine locked {len(criteria)} objective discharge criteria "
                       f"but no discharge/confinement release conditions were found in "
                       f"Study Design, Study Assessments or Safety Monitoring.",
                section="study_design"))
        elif mandatory and not _authorised_discharge(disc_win):
            add(QCFinding(id="safety_discharge", category="Safety",
                title="Discharge lacks documented physician authorisation",
                status=QCStatus.FAIL,
                detail="Discharge conditions are described, but the mandatory criterion "
                       "of documented written authorisation by the Principal "
                       "Investigator (or a medically-qualified delegate) is not among "
                       "them. Without it a dosed subject can leave the unit on a "
                       "checklist reading alone.",
                section="study_design"))
        else:
            add(QCFinding(id="safety_discharge", category="Safety",
                title="Discharge criteria present", status=QCStatus.PASS,
                detail=f"Discharge conditions are stated, including the required "
                       f"documented physician authorisation ({len(criteria)} locked "
                       f"criteria).",
                section="study_design"))

    # (e) individual vs cohort rules must stay separate.
    if halt_win and not re.search(
            r"withdraw|discontinu|remove(?:d)? from the study|individual subject",
            safety, re.IGNORECASE):
        add(QCFinding(id="safety_rule_levels", category="Safety",
            title="Individual stopping rules not distinguished from cohort halting",
            status=QCStatus.WARNING,
            detail="The Safety Monitoring text describes halting dosing but never "
                   "describes withdrawing or discontinuing an individual subject. The "
                   "two levels answer different questions and merging them is the exact "
                   "error the gating engine exists to prevent.",
            section="safety_monitoring"))

    # (f) a detected boxed warning has to be acknowledged somewhere.
    lf = (gating.get("label_findings")
          if isinstance(gating.get("label_findings"), dict) else {})
    if lf.get("boxed_warning_detected"):
        scan = _joined(sections, "study_rationale", "introduction", "population",
                       "safety_monitoring", "synopsis").lower()
        if "boxed warning" not in scan and "black box" not in scan:
            add(QCFinding(id="safety_boxed_warning", category="Safety",
                title="Boxed warning detected but never mentioned", status=QCStatus.WARNING,
                detail=f"A boxed warning was detected in the product label "
                       f"({lf.get('boxed_warning_section') or 'label text'}), but no "
                       f"section mentions it. The risk/benefit rationale must address it.",
                section="study_rationale"))


# --- 12. DDI screening: the exclusion list must be drug-specific ------------

# Text cues for each pathway the screening engine can emit. Matched against the
# GENERATED text so a clause naming an enzyme the drug does not use can be
# separated from one naming an enzyme it does.
_PATHWAY_TEXT_CUES: tuple[tuple[str, str], ...] = (
    ("cyp3a4", r"cyp\s*-?\s*3a(?:4|5)?\b"),
    ("cyp2d6", r"cyp\s*-?\s*2d6\b"),
    ("cyp2c19", r"cyp\s*-?\s*2c19\b"),
    ("cyp2c9", r"cyp\s*-?\s*2c9\b"),
    ("cyp2c8", r"cyp\s*-?\s*2c8\b"),
    ("cyp1a2", r"cyp\s*-?\s*1a2\b"),
    ("cyp2b6", r"cyp\s*-?\s*2b6\b"),
    ("ugt", r"\bugt\b|glucuronosyltransferase"),
    ("pgp", r"\bp-?\s?gp\b|p-?glycoprotein"),
    ("bcrp", r"\bbcrp\b|breast cancer resistance protein"),
    ("oatp", r"\boatp\b|organic anion[- ]transporting"),
    ("oct_mate", r"\boct[12]\b|\bmate-?[12k]?\b|multidrug and toxin extrusion"),
)

_PATHWAY_DISPLAY = {
    "cyp3a4": "CYP3A4", "cyp2d6": "CYP2D6", "cyp2c19": "CYP2C19",
    "cyp2c9": "CYP2C9", "cyp2c8": "CYP2C8", "cyp1a2": "CYP1A2",
    "cyp2b6": "CYP2B6", "ugt": "UGT", "pgp": "P-glycoprotein",
    "bcrp": "BCRP", "oatp": "OATP", "oct_mate": "OCT/MATE",
}

_PROHIBITION_KEYWORDS = ("prohibit", "exclu", "must not", "may not", "not permitted",
                         "restrict", "concomitant", "withhold", "disallow",
                         "forbidden", "washout", "washed out", "discontinu")


def _named_pathways(text: str) -> set[str]:
    """Pathway keys the text names inside a prohibition/exclusion clause.

    Scoped to prohibition language, and negated mentions ("the drug is not a
    CYP3A4 substrate") are ignored, so a factual statement about metabolism is
    not read as a copy-pasted exclusion.
    """
    win = _windows(text, _PROHIBITION_KEYWORDS, radius=260)
    if not win:
        return set()
    found: set[str] = set()
    for key, pat in _PATHWAY_TEXT_CUES:
        for m in re.finditer(pat, win, re.IGNORECASE):
            if _NEGATED_RE.search(_sentence_around(win, m.start(), m.end())):
                continue
            found.add(key)
            break
    return found


def _check_ddi(add: _Add, sections: dict[str, str],
               population_config: dict[str, Any]) -> None:
    """The prohibited-medication list must belong to THIS drug.

    A generic "no strong CYP3A4 inhibitors or inducers" clause is the single
    most-copied paragraph in the industry, and for a drug cleared by CYP2C9 it
    excludes the wrong medicines while permitting the ones that will actually
    move the exposure. The engine derives the pathways from the real label text;
    this check proves the narrative did not fall back on the boilerplate.
    """
    ddi = _facts(population_config, "_ddi")
    if not ddi:
        _engine_absent(add, id="ddi_facts", category="Interactions",
                       title="Concomitant-medication list not verified",
                       engine="drug-interaction screening", section="population")
        return

    text = _joined(sections, "population", "treatment", "synopsis", "study_assessments")
    if not text:
        add(QCFinding(id="ddi_specificity", category="Interactions",
            title="No text to check the exclusion list against", status=QCStatus.WARNING,
            detail="Population and Treatment are empty, so the prohibited-medication "
                   "list could not be verified against the screened pathways.",
            section="population"))
        return

    keys = {k for k in (ddi.get("pathway_keys") or []) if isinstance(k, str)}
    named = _named_pathways(text)
    foreign = sorted(named - keys)
    covered = sorted(keys & named)
    uncovered = sorted(keys - named)
    unresolved = [u for u in (ddi.get("unresolved") or []) if u]

    def _disp(ks: list[str]) -> str:
        return ", ".join(_PATHWAY_DISPLAY.get(k, k.upper()) for k in ks)

    if foreign and not keys:
        add(QCFinding(id="ddi_specificity", category="Interactions",
            title="Generic enzyme boilerplate in the exclusion criteria",
            status=QCStatus.FAIL,
            detail=f"The exclusion/concomitant-medication text prohibits perpetrators of "
                   f"{_disp(foreign)}, but no clearance pathway could be identified for "
                   f"this drug from its label, so no isoform-specific prohibition is "
                   f"justified. This is a copied clause, not a screened one: it excludes "
                   f"the wrong medicines and permits the ones that would actually shift "
                   f"exposure. Use the engine's conservative pan-pathway wording and its "
                   f"disclosure instead.",
            section="population"))
    elif foreign:
        add(QCFinding(id="ddi_specificity", category="Interactions",
            title="Exclusion list names a pathway this drug does not use",
            status=QCStatus.FAIL,
            detail=f"The text prohibits or restricts perpetrators of {_disp(foreign)}, "
                   f"but the screened clearance pathways for this drug are "
                   f"{_disp(sorted(keys))}. Remove the unscreened clause or justify it "
                   f"from the label.",
            section="population"))
    elif keys and not named:
        add(QCFinding(id="ddi_specificity", category="Interactions",
            title="Screened pathways never named in the exclusions",
            status=QCStatus.WARNING,
            detail=f"The engine screened {_disp(sorted(keys))} from the product label, "
                   f"but no exclusion or concomitant-medication clause names any of "
                   f"them. The prohibitions cannot be applied at screening if the site "
                   f"is not told which class to look for.",
            section="population"))
    elif covered:
        add(QCFinding(id="ddi_specificity", category="Interactions",
            title="Exclusion list is drug-specific", status=QCStatus.PASS,
            detail=f"Every enzyme/transporter named in the prohibition clauses "
                   f"({_disp(covered)}) is one the screening engine derived from this "
                   f"drug's label.",
            section="population"))
    if uncovered and covered:
        add(QCFinding(id="ddi_coverage", category="Interactions",
            title="Some screened pathways are not in the exclusions",
            status=QCStatus.WARNING,
            detail=f"{_disp(uncovered)} was/were screened from the label but no "
                   f"prohibition clause names it/them.",
            section="population"))

    # Diet and lifestyle rules are pathway-specific and are the other routinely
    # copied clause. The engine emits grapefruit ONLY for a CYP3A4 substrate.
    row_keys = {
        r.get("key") for r in _rows(ddi, "prohibited") + _rows(ddi, "restricted")
    }
    low = text.lower()
    if "grapefruit" in low and "grapefruit" not in row_keys:
        add(QCFinding(id="ddi_grapefruit", category="Interactions",
            title="Grapefruit prohibited without a CYP3A4 basis", status=QCStatus.FAIL,
            detail="The text restricts grapefruit, but the screening engine did not "
                   "emit a grapefruit rule for this drug - grapefruit furanocoumarins "
                   "act on intestinal CYP3A4 and are irrelevant to a drug cleared by "
                   "another route. This is boilerplate carried over from another "
                   "protocol.",
            section="population"))
    # --- Biologic-specific -------------------------------------------------
    # A therapeutic protein is catabolised by proteolysis, so a CYP or
    # transporter restriction can only be boilerplate; and subjects MUST be
    # naive to the molecule, because pre-existing anti-drug antibodies both
    # depress exposure and make treatment-emergent immunogenicity unscoreable.
    if str(ddi.get("modality") or "").lower() == "biologic":
        cyp_win = _windows(
            text, ("cyp", "cytochrome", "p450", "transporter", "p-gp", "oatp"), radius=200)
        if cyp_win and re.search(
                r"prohibit|not permitted|must not|washout|exclu|restrict", cyp_win, re.I):
            add(QCFinding(id="ddi_biologic_cyp", category="Interactions",
                title="CYP or transporter restriction imposed for a biologic",
                status=QCStatus.FAIL,
                detail="The text imposes a cytochrome-P450 or drug-transporter "
                       "restriction, but the study drug is a therapeutic protein. It is "
                       "cleared by proteolytic catabolism and FcRn recycling, not by "
                       "P450 enzymes or the transporters those clauses name, so the "
                       "restriction cannot apply and is boilerplate carried over from a "
                       "small-molecule protocol. Stating the drug is NOT a P450 "
                       "substrate is correct; restricting comedications on that basis "
                       "is not.",
                section="population"))
        if ddi.get("requires_naive_subjects"):
            naive_win = _windows(
                text, ("naive", "naïve", "prior exposure", "previously received",
                       "previous exposure", "anti-drug antibod", "pre-existing"),
                radius=260)
            if not (naive_win and re.search(
                    r"exclu|must not|prohibit|ineligible|not eligible", naive_win, re.I)):
                add(QCFinding(id="ddi_biologic_naive", category="Population",
                    title="Biologic-naive requirement missing from exclusion criteria",
                    status=QCStatus.FAIL,
                    detail="This is a biologic PK-similarity study, so subjects must be "
                           "naive to the reference product, the proposed biosimilar and "
                           "any other biologic against the same target - but no such "
                           "exclusion appears. Prior exposure can leave anti-drug "
                           "antibodies that persist for years; they accelerate clearance "
                           "and depress exposure, biasing the Test/Reference comparison "
                           "in whichever arm the affected subject falls, and a "
                           "baseline-positive subject cannot be scored for "
                           "treatment-emergent immunogenicity at all. The exclusion must "
                           "be stated and confirmed by a baseline ADA sample.",
                    section="population"))

    smoke_win = _windows(text, ("tobacco", "smok", "caffeine", "xanthine"), radius=200)
    if smoke_win and re.search(r"cyp|induc|inhibit|metaboli", smoke_win):
        if "tobacco" not in row_keys and "xanthines" not in row_keys:
            add(QCFinding(id="ddi_lifestyle", category="Interactions",
                title="Tobacco/caffeine restricted on a metabolic rationale that was "
                      "not screened",
                status=QCStatus.WARNING,
                detail="The text ties a tobacco or caffeine restriction to enzyme "
                       "induction/inhibition, but CYP1A2 was not among the screened "
                       "pathways. Keeping the restriction as a standardised-conditions "
                       "rule is fine; presenting it as a metabolic interaction is not.",
                section="population"))

    # A washout figure in a comedication clause has to be one the engine emitted.
    engine_days = {
        _f(r.get("washout_days")) for r in
        _rows(ddi, "prohibited") + _rows(ddi, "restricted")
    }
    for r in _rows(ddi, "prohibited") + _rows(ddi, "restricted"):
        ext = r.get("extended_washout")
        if isinstance(ext, list):
            for e in ext:
                if isinstance(e, dict):
                    engine_days.add(_f(e.get("washout_days")))
    engine_days.add(_f(ddi.get("max_washout_days")))
    engine_days.add(_f(ddi.get("screening_history_window_days")))
    # The BETWEEN-PERIOD washout is a different number with the same name; it is
    # checked in section 4 and must not be flagged here.
    ss_block = _facts(population_config, "_sample_size")
    t_half = _f(ss_block.get("t_half_hours"))
    if t_half:
        engine_days.add(round(7 * t_half / 24.0, 4))
    engine_days.discard(None)
    if engine_days:
        med_win = _windows(text, ("prohibit", "concomitant", "not permitted",
                                  "must not", "exclu"), radius=220)
        stray: set[float] = set()
        for m in re.finditer(
                r"(\d{1,3}(?:\.\d+)?)\s*(?:calendar\s+)?days?\s+(?:prior to|before|"
                r"preceding|of the|ahead of)", med_win):
            v = _f(m.group(1))
            if v is None:
                continue
            if not any(abs(v - d) <= 0.51 for d in engine_days):
                stray.add(v)
        if stray:
            add(QCFinding(id="ddi_washout", category="Interactions",
                title="Comedication washout not traceable to the screening engine",
                status=QCStatus.WARNING,
                detail=f"The concomitant-medication clauses state washout(s) of "
                       f"{', '.join(f'{v:g}' for v in sorted(stray))} day(s), which are "
                       f"not among the values the screening engine emitted "
                       f"({', '.join(f'{d:g}' for d in sorted(engine_days))} days). "
                       f"Confirm the figure or restore the locked one.",
                section="population"))

    if unresolved:
        # Two very different things land in `unresolved`. When NO pathway could
        # be identified the engine fell back to a conservative pan-pathway
        # prohibition, and the protocol must say so or a reader takes a guess for
        # a screened result. When pathways WERE identified, the entries are the
        # engine's own caveats (e.g. no clinically established inducer exists for
        # renal cation transporters) - correct, disclosed, and not a defect.
        scan = text.lower()
        disclosed = ("could not be identified" in scan
                     or "conservative" in scan
                     or "pan-pathway" in scan
                     or "clinical pharmacologist" in scan)
        fallback = not keys
        add(QCFinding(id="ddi_unresolved", category="Interactions",
            title=("Interaction screening unresolved - conservative fallback in use"
                   if fallback else "Interaction screening caveats to disclose"),
            status=(QCStatus.WARNING if (fallback and not disclosed)
                    else QCStatus.ASSUMPTION),
            detail=" ".join(str(u) for u in unresolved)
                   + ((" This is disclosed in the generated text." if disclosed else
                       " The generated text does not disclose it; a reader would take "
                       "the conservative fallback list for a screened result.")
                      if fallback else
                      " Confirm these caveats are reflected in the protocol."),
            section="population"))


# --- 13. Randomization: method stated, allocation not leaked ----------------

_BLOCK_SIZE_RE = re.compile(
    r"block(?:s)?\s*(?:size)?\s*(?:of|=|:)?\s*(\d{1,3})\b"
    r"|block\s+size\s+of\s+(\d{1,3})\b",
    re.IGNORECASE,
)
_SEQUENCE_TOKEN_RE = re.compile(r"\b[TR]{2,6}\b")


def _check_randomization(add: _Add, sections: dict[str, str],
                         population_config: dict[str, Any]) -> None:
    """Two opposite failures, both checked here.

    The protocol must SAY enough - the method, the stratification, the sequence
    set - and must not say too much. The allocation list, the seed that
    regenerates it and the block length are allocation-concealment material
    (ICH E9 sect. 2.3.2): a block length in the protocol body makes the last
    allocation of every block deducible, and the seed rebuilds the entire list.
    """
    rand = _facts(population_config, "_randomization")
    if not rand:
        _engine_absent(add, id="randomization_facts", category="Randomization",
                       title="Randomization schedule not verified",
                       engine="randomization", section="treatment")
        return

    body = _joined(sections, "synopsis", "study_design", "treatment", "population",
                   "study_assessments", "statistical_analysis_plan", "data_handling")
    if not body:
        add(QCFinding(id="randomization_method", category="Randomization",
            title="No text to check the randomization against", status=QCStatus.WARNING,
            detail="The design/treatment sections are empty, so neither the stated "
                   "method nor a possible allocation leak could be checked.",
            section="treatment"))
        return
    low = body.lower()

    # (a) LEAK CHECK - the highest-consequence line in this module.
    block = rand.get("block") if isinstance(rand.get("block"), dict) else {}
    repro = (rand.get("reproducibility")
             if isinstance(rand.get("reproducibility"), dict) else {})
    ua = (rand.get("unblinded_allocation")
          if isinstance(rand.get("unblinded_allocation"), dict) else {})
    bsize = block.get("size") if isinstance(block.get("size"), int) else None
    seed = repro.get("seed") if isinstance(repro.get("seed"), int) else None

    stated_blocks = set()
    for m in _BLOCK_SIZE_RE.finditer(body):
        for g in m.groups():
            if g:
                v = _f(g)
                if v is not None:
                    stated_blocks.add(int(v))

    leaks: list[str] = []
    if bsize is not None and bsize in stated_blocks:
        leaks.append(f"the block length ({bsize})")
    if seed is not None and re.search(r"(?<![\d.])%d(?![\d.])" % seed, body):
        leaks.append(f"the randomization seed ({seed})")
    detail_str = block.get("method_detail")
    if isinstance(detail_str, str) and detail_str:
        needle = detail_str.split(":", 1)[-1].strip().lower()[:40]
        if needle and needle in low:
            leaks.append("the restricted block-composition statement")
    ids = [
        e.get("subject_id") for e in _rows(ua, "entries")
        if isinstance(e.get("subject_id"), str)
    ]
    hit_ids = [sid for sid in ids if re.search(r"\b%s\b" % re.escape(sid), body)]
    if len(hit_ids) >= 3:
        leaks.append(f"{len(hit_ids)} subject identifiers from the restricted "
                     f"allocation list (e.g. {', '.join(hit_ids[:3])})")

    if leaks:
        add(QCFinding(id="randomization_leak", category="Randomization",
            title="Allocation concealment breached in the protocol body",
            status=QCStatus.FAIL,
            detail="The generated text discloses " + "; ".join(leaks) + ". "
                   "Under ICH E9 sect. 2.3.2 none of these may appear in the protocol, "
                   "the synopsis, or anything circulated to investigators - each one "
                   "lets a reader predict upcoming allocations. They belong in the "
                   "restricted randomization record in the trial master file.",
            section="treatment"))
    elif stated_blocks and bsize is None:
        add(QCFinding(id="randomization_leak", category="Randomization",
            title="Protocol body states a block length", status=QCStatus.WARNING,
            detail=f"The text states a randomization block size "
                   f"({', '.join(str(b) for b in sorted(stated_blocks))}). The block "
                   f"length is withheld from the protocol body regardless of its value, "
                   f"because it makes each block's final allocation deducible.",
            section="treatment"))
    elif stated_blocks:
        add(QCFinding(id="randomization_leak", category="Randomization",
            title="Protocol body states a block length that is not the locked one",
            status=QCStatus.WARNING,
            detail=f"The text states a block size of "
                   f"{', '.join(str(b) for b in sorted(stated_blocks))} while the locked "
                   f"schedule uses a different length. Both problems apply: the figure "
                   f"is wrong, and no figure should be stated at all.",
            section="treatment"))
    else:
        add(QCFinding(id="randomization_leak", category="Randomization",
            title="No allocation-concealment leak detected", status=QCStatus.PASS,
            detail="The block length, the seed and the subject-level allocation list do "
                   "not appear in the generated sections.",
            section="treatment"))

    # (b) the method must actually be described.
    if re.search(r"randomi[sz]", low) and re.search(r"permuted block|block randomi|"
                                                    r"randomly permuted|blocked randomi",
                                                    low):
        add(QCFinding(id="randomization_method", category="Randomization",
            title="Randomization method stated", status=QCStatus.PASS,
            detail="The text describes a computer-generated permuted-block "
                   "randomization schedule.",
            section="treatment"))
    elif re.search(r"randomi[sz]", low):
        add(QCFinding(id="randomization_method", category="Randomization",
            title="Randomization method not described", status=QCStatus.WARNING,
            detail="Randomization is mentioned but the method is not stated. The "
                   "protocol body should say the schedule is computer-generated using "
                   "randomly permuted blocks of undisclosed length.",
            section="treatment"))
    else:
        add(QCFinding(id="randomization_method", category="Randomization",
            title="Randomization never described", status=QCStatus.FAIL,
            detail="A randomization schedule was generated for this study but no "
                   "section describes how subjects are allocated.",
            section="treatment"))

    # (c) the sequence set must be the locked one.
    design = rand.get("design") if isinstance(rand.get("design"), dict) else {}
    seqs = [s for s in (design.get("sequences") or []) if isinstance(s, str)]
    if seqs:
        locked = {s.upper() for s in seqs}
        found = {m.group(0).upper() for m in _SEQUENCE_TOKEN_RE.finditer(body)}
        wrong = sorted(f for f in found if f not in locked and len(f) == len(seqs[0]))
        if wrong:
            add(QCFinding(id="randomization_sequences", category="Randomization",
                title="Treatment sequences differ from the locked design",
                status=QCStatus.WARNING,
                detail=f"The text names sequence(s) {', '.join(wrong)} while the locked "
                       f"design uses {', '.join(sorted(locked))}. A subject cannot be "
                       f"allocated to a sequence the schedule does not contain.",
                section="treatment"))
        elif locked & found:
            add(QCFinding(id="randomization_sequences", category="Randomization",
                title="Treatment sequences match the locked design", status=QCStatus.PASS,
                detail=f"The text names the locked sequence set "
                       f"({', '.join(sorted(locked))}).",
                section="treatment"))
        else:
            add(QCFinding(id="randomization_sequences", category="Randomization",
                title="Treatment sequences not stated", status=QCStatus.WARNING,
                detail=f"The locked sequence set ({', '.join(sorted(locked))}) does not "
                       f"appear in the generated text.",
                section="treatment"))

    # (d) stratification claimed vs implemented.
    strat = (rand.get("stratification")
             if isinstance(rand.get("stratification"), dict) else {})
    if strat:
        applied = bool(strat.get("applied"))
        rand_win = _windows(body, ("randomi",), radius=400)
        claims = bool(re.search(r"stratif", rand_win)) if rand_win else False
        if applied and not claims:
            factors = [
                str(f.get("label") or f.get("key"))
                for f in _rows(strat, "factors")
            ]
            add(QCFinding(id="randomization_stratification", category="Randomization",
                title="Stratified schedule not described as stratified",
                status=QCStatus.WARNING,
                detail=f"The schedule is stratified on {len(factors)} factor(s)"
                       + (f" ({'; '.join(factors)})" if factors else "")
                       + ", but the randomization narrative does not say so. The site "
                         "must know which stratum a subject enters before allocation.",
                section="treatment"))
        elif claims and not applied:
            add(QCFinding(id="randomization_stratification", category="Randomization",
                title="Stratification claimed but not implemented",
                status=QCStatus.WARNING,
                detail="The randomization narrative describes stratified allocation, but "
                       "the locked schedule is unstratified (no factor met the minimum "
                       "of one complete block per stratum at this sample size).",
                section="treatment"))
        else:
            add(QCFinding(id="randomization_stratification", category="Randomization",
                title="Stratification statement matches the schedule", status=QCStatus.PASS,
                detail=("Stratified allocation is both implemented and described."
                        if applied else
                        "The schedule is unstratified and the text does not claim "
                        "otherwise."),
                section="treatment"))

    # (e) blinding claim vs the assessed masking posture.
    blinding = rand.get("blinding") if isinstance(rand.get("blinding"), dict) else {}
    masking = (blinding.get("treatment_masking")
               if isinstance(blinding.get("treatment_masking"), dict) else {})
    recommended = str(masking.get("recommended") or "")
    if recommended.startswith("open_label") and re.search(r"double[- ]blind", low):
        add(QCFinding(id="randomization_blinding", category="Randomization",
            title="Protocol claims double-blind against an open-label posture",
            status=QCStatus.WARNING,
            detail=f"The blinding assessment recommends "
                   f"'{recommended.replace('_', ' ')}' (the endpoint is an instrument "
                   f"reading, and the bioanalytical laboratory is what must be blinded), "
                   f"but the text describes a double-blind study. Claiming masking that "
                   f"the conduct does not implement is a deviation waiting to be "
                   f"written.",
            section="study_design"))
