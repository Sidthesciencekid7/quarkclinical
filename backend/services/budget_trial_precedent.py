"""Real-trial-history grounding for the Budgeting Tool (services/budget_engine.py).

Fixes the "same dollar amount for any drug" gap: services/budget_rate_card.py's
DEFAULT_STUDY_PROFILES / PATIENT_STUDY_PROFILES supply fixed n_enrolled /
n_sites / n_countries values keyed ONLY on study_type (+ therapeutic_area),
with zero variation by the actual drug being budgeted — two different drugs
of the same modality get identical quantity drivers, and therefore identical
line items, today. This module closes that gap for the quantities a real
trial registry can actually answer (enrollment size, site/country footprint)
by mining THIS drug's own real ClinicalTrials.gov history.

Deliberately NOT the same thing as services/trial_precedent.py, even though
both consume the same LiteratureService.fetch_clinicaltrials() cache: that
module answers "what inclusion/exclusion CRITERIA TEXT has precedent for this
indication" (the Protocol Generator's Population-section grounding). This
module answers a different question — "what enrollment size / site count /
country count / duration did THIS drug's OWN real trials actually have" — a
purely quantitative signal neither trial_precedent.TrialPrecedent nor its
selection logic captures.

Scope is deliberately honest, not exhaustive: there is no public, searchable
database of historical CRO BUDGETS for arbitrary drugs (the rate card's unit
costs are, and remain, calibrated from the specific real bids the user
supplied — see budget_rate_card.SOURCE_REGISTRY["src.kshb012_bids"] and
siblings). What real trial registries DO reliably carry is trial DESIGN —
enrollment, site/country footprint, phase, status — which is exactly the
per-drug signal the generic per-study-type default was missing. A drug with
no usable ClinicalTrials.gov history (sparse registration, or a molecule with
no prior human trials) honestly falls back to the generic default with that
fact recorded in the derivation audit trail, never a fabricated match.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from services.literature_service import ClinicalTrialRecord

# Same ranking convention as services/trial_precedent.py's _STATUS_RANK:
# completed trials are the most reliable signal (an ACTUAL, not ESTIMATED,
# enrollment count; a real, not projected, duration), so they sort first when
# capping the precedent set.
_STATUS_RANK: dict[str, int] = {
    "COMPLETED": 0,
    "ACTIVE_NOT_RECRUITING": 1,
    "ENROLLING_BY_INVITATION": 2,
    "RECRUITING": 3,
    "NOT_YET_RECRUITING": 4,
    "UNKNOWN": 5,
    "SUSPENDED": 6,
    "TERMINATED": 7,
    "WITHDRAWN": 8,
}

MAX_PRECEDENT_TRIALS = 8
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")


def _duration_months(start_date: str, completion_date: str) -> float | None:
    """CT.gov dates are "YYYY-MM" or "YYYY-MM-DD"; day is dropped either way
    since a month-granularity duration is all the budget engine ever uses.
    Returns None for missing/malformed/non-positive spans rather than a
    fabricated 0 or negative number."""
    start_m = _DATE_RE.match((start_date or "").strip())
    end_m = _DATE_RE.match((completion_date or "").strip())
    if not start_m or not end_m:
        return None
    start_months = int(start_m.group(1)) * 12 + int(start_m.group(2))
    end_months = int(end_m.group(1)) * 12 + int(end_m.group(2))
    span = end_months - start_months
    return float(span) if span > 0 else None


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).strip()


def _indication_matches(indication: str | None, conditions: list[str]) -> bool:
    """Bidirectional substring match, same heuristic as
    services.trial_precedent._condition_matches — a real trial registry's
    free-text condition field is never going to exactly equal a protocol's
    normalized indication string, so containment in either direction is the
    workable bar. No indication supplied (a healthy-volunteer BE/PK study,
    which has no indication concept) matches everything — for that case a
    drug's own trial history is relevant regardless of which indication any
    individual trial targeted."""
    if not indication:
        return True
    norm_ind = _normalize(indication)
    if not norm_ind:
        return True
    for cond in conditions:
        norm_cond = _normalize(cond)
        if not norm_cond:
            continue
        if norm_ind in norm_cond or norm_cond in norm_ind:
            return True
    return False


@dataclass(frozen=True)
class BudgetTrialPrecedent:
    nct_id: str
    title: str
    status: str
    phase: str
    enrollment_count: int | None
    enrollment_type: str
    n_countries: int
    n_locations: int
    duration_months: float | None


@dataclass(frozen=True)
class BudgetPrecedentResult:
    precedents: tuple[BudgetTrialPrecedent, ...] = field(default_factory=tuple)
    median_n_enrolled: int | None = None
    median_n_countries: int | None = None
    median_n_sites: int | None = None
    median_duration_months: float | None = None

    @property
    def found(self) -> bool:
        return bool(self.precedents)


NO_PRECEDENT = BudgetPrecedentResult()


def _phase_matches(subject_profile: str, phase: str) -> bool:
    """A drug tested in BOTH a small healthy-volunteer PK study and a much
    larger later-phase patient trial is common (the same intervention name
    covers both) — without a phase-consistency filter, blending a 24-subject
    Phase 1 BE trial with a 300-subject Phase 3 oncology trial into one
    median would produce a WORSE number than the generic default, not a
    better one. "" (no phase reported — observational or some early trials)
    is treated as compatible with a healthy-volunteer lookup only, since a
    patient-study quantity driver needs at least a nominal phase to trust."""
    phase = (phase or "").upper()
    if subject_profile == "patient":
        return any(p in phase for p in ("2", "3", "4"))
    return phase == "" or "1" in phase or "EARLY_PHASE1" in phase


def select_budget_precedents(
    *, indication: str | None, subject_profile: str, trials: list[ClinicalTrialRecord],
) -> list[BudgetTrialPrecedent]:
    """Filter this drug's raw (cached, unfiltered) ClinicalTrials.gov history
    down to trials usable as a quantity-driver precedent, ranked by status
    quality and capped at MAX_PRECEDENT_TRIALS. A trial with no usable
    quantitative signal at all (no enrollment count AND no location data) is
    dropped — it cannot inform any driver this module resolves. Phase must
    also be consistent with `subject_profile` (see _phase_matches) — the same
    drug's healthy-volunteer PK history and patient-trial history are NOT
    interchangeable precedent for one another."""
    candidates = [
        t for t in trials
        if _indication_matches(indication, t.get("conditions") or [])
        and _phase_matches(subject_profile, t.get("phase") or "")
        and (t.get("enrollment_count") is not None or t.get("countries"))
    ]
    candidates.sort(key=lambda t: _STATUS_RANK.get(t.get("status") or "", 9))

    out: list[BudgetTrialPrecedent] = []
    for t in candidates[:MAX_PRECEDENT_TRIALS]:
        out.append(BudgetTrialPrecedent(
            nct_id=t["nct_id"],
            title=t.get("title") or "",
            status=t.get("status") or "",
            phase=t.get("phase") or "",
            enrollment_count=t.get("enrollment_count"),
            enrollment_type=t.get("enrollment_type") or "",
            n_countries=len(t.get("countries") or []),
            n_locations=t.get("n_locations") or 0,
            duration_months=_duration_months(
                t.get("start_date") or "", t.get("primary_completion_date") or "",
            ),
        ))
    return out


def aggregate_budget_drivers(precedents: list[BudgetTrialPrecedent]) -> BudgetPrecedentResult:
    """Median across precedents with a usable value for each field
    independently — a precedent missing enrollment but carrying country data
    (or vice versa) still contributes to whichever median it can. ACTUAL
    enrollment counts are preferred over ESTIMATED when both are present
    across the precedent set (a completed trial's real enrollment beats a
    still-running trial's target)."""
    if not precedents:
        return NO_PRECEDENT

    actual_enrolled = [p.enrollment_count for p in precedents
                        if p.enrollment_count is not None and p.enrollment_type == "ACTUAL"]
    any_enrolled = [p.enrollment_count for p in precedents if p.enrollment_count is not None]
    enrolled_pool = actual_enrolled or any_enrolled
    median_n_enrolled = round(statistics.median(enrolled_pool)) if enrolled_pool else None

    countries_pool = [p.n_countries for p in precedents if p.n_countries > 0]
    median_n_countries = round(statistics.median(countries_pool)) if countries_pool else None

    sites_pool = [p.n_locations for p in precedents if p.n_locations > 0]
    median_n_sites = round(statistics.median(sites_pool)) if sites_pool else None

    duration_pool = [p.duration_months for p in precedents if p.duration_months is not None]
    median_duration_months = round(statistics.median(duration_pool), 1) if duration_pool else None

    return BudgetPrecedentResult(
        precedents=tuple(precedents),
        median_n_enrolled=median_n_enrolled,
        median_n_countries=median_n_countries,
        median_n_sites=median_n_sites,
        median_duration_months=median_duration_months,
    )
