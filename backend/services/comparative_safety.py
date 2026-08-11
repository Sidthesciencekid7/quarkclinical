"""Study duration for a biosimilar PK-similarity study: how many doses, and
how long the head-to-head safety/immunogenicity comparison must run.

WHY THIS EXISTS
---------------
Two duration questions decide the shape of a multiple-dose biosimilar study,
and they have DIFFERENT answers, which is the point of computing them together:

  1. HOW MANY DOSES until steady state? This sets when the steady-state
     AUC(0-tau) co-primary endpoint can be read. Read it too early and the
     "steady-state" comparison is really a comparison of two accumulation
     curves caught mid-climb, which is neither the first-dose endpoint nor the
     steady-state one.

  2. HOW LONG must the head-to-head safety and immunogenicity comparison run?
     Much longer than the PK readout. Anti-drug antibodies take months to
     develop and their incidence is the whole point of a comparative safety
     assessment, so the study does not end when the PK endpoint is read.

The consequence — and the thing a protocol commonly gets wrong — is that the
PRIMARY ENDPOINT IS READ WELL BEFORE THE STUDY ENDS. The protocol therefore
needs an explicit primary analysis timepoint, a distinct end-of-study
timepoint, and a statement that subjects remain in comparative follow-up
between them. Emitting both numbers from one engine keeps them consistent.

REGULATORY DURATION
-------------------
A comparative safety and immunogenicity assessment is conventionally at least
6 MONTHS head-to-head. The EU expectation for a biosimilar comparative safety
package has been 12 MONTHS. Since this application's biosimilar studies use the
EU reference medicinal product as comparator (see services/guidelines.py), the
programme is on a path where EU data expectations apply, so the default here is
12 months with the 6-month floor stated explicitly. A sponsor filing only in the
US may shorten to 6 months, and the engine reports both so the choice is a
decision rather than an oversight.

Follows the deterministic-engine pattern: computed once in Python, injected as
locked facts, never re-derived by the model.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# Fraction of steady state reached after n half-lives: 1 - 2^-n.
#   4 half-lives -> 93.8%   5 -> 96.9%   6 -> 98.4%   7 -> 99.2%
# Five is the conventional threshold for "at steady state" and is used here.
HALF_LIVES_TO_STEADY_STATE = 5.0

# "Steady state in ALL patients", not in the median patient. Between-subject
# variability in clearance means the slowest patients reach steady state later
# than a median-half-life calculation predicts, so the median half-life is
# inflated by this factor before the dose count is computed. 1.5x approximates
# the upper end of a typical monoclonal-antibody half-life distribution.
_SLOW_CLEARER_THALF_FACTOR = 1.5

# Steady state cannot be DEMONSTRATED with fewer than three pre-dose troughs
# (you need at least three points to show the concentrations are no longer
# rising), regardless of what the half-life arithmetic alone would allow.
MIN_DOSES_FOR_STEADY_STATE = 3
MIN_TROUGHS_TO_DEMONSTRATE = 3

# Head-to-head comparative safety / immunogenicity duration.
SAFETY_MONTHS_FLOOR = 6
SAFETY_MONTHS_EU = 12


@dataclass
class DurationPlan:
    applicable: bool
    reason: str = ""
    # --- steady state -------------------------------------------------
    t_half_hours: Optional[float] = None
    t_half_basis: str = ""
    tau_hours: Optional[float] = None
    doses_to_steady_state: Optional[int] = None
    steady_state_day: Optional[float] = None
    troughs_to_demonstrate: int = MIN_TROUGHS_TO_DEMONSTRATE
    steady_state_rationale: str = ""
    # --- comparative safety -------------------------------------------
    safety_months: int = SAFETY_MONTHS_EU
    safety_floor_months: int = SAFETY_MONTHS_FLOOR
    safety_basis: str = ""
    primary_readout_note: str = ""
    immunogenicity_timepoints_months: list[float] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def doses_to_steady_state(
    t_half_hours: Optional[float],
    tau_hours: Optional[float],
    *,
    cover_all_patients: bool = True,
) -> Optional[int]:
    """Minimum number of doses before steady state can be assumed.

    Dose 1 is given at t=0, so dose n is given at t=(n-1)*tau — n doses cover
    only (n-1) COMPLETE dosing intervals, not n. The steady-state profile is
    collected over the interval FOLLOWING dose n (build_duration_plan below),
    so what must have elapsed by t=(n-1)*tau, when that dose is given, is the
    full 5 half-lives: (n-1)*tau >= 5*t_half, i.e. n >= 5*t_half/tau + 1. Naively
    using n = ceil(5*t_half/tau) with no "+1" (an earlier version of this
    function did exactly that) systematically UNDER-counts by one dose - across
    a realistic half-life/tau grid it attains only 4.00-4.99 inflated
    half-lives, never the full 5, so the "steady-state" profile would actually
    be collected while accumulation is still incomplete.

    ``n = ceil(5 x t_half / tau) + 1``, floored at MIN_DOSES_FOR_STEADY_STATE.
    When ``cover_all_patients`` the half-life is first inflated by
    _SLOW_CLEARER_THALF_FACTOR, because the endpoint has to be at steady state
    in every subject, not in the median one — a subject who clears the product
    slowly is still accumulating at the dose number a median calculation picks,
    and their "steady-state" AUC would be biased low.

    Returns None when either input is missing, so the caller reports the gap
    rather than inventing a dose count.
    """
    if not t_half_hours or not tau_hours or t_half_hours <= 0 or tau_hours <= 0:
        return None
    t_half = float(t_half_hours) * (_SLOW_CLEARER_THALF_FACTOR if cover_all_patients else 1.0)
    n = math.ceil(HALF_LIVES_TO_STEADY_STATE * t_half / float(tau_hours)) + 1
    return max(int(n), MIN_DOSES_FOR_STEADY_STATE)


def immunogenicity_schedule(
    months: int, *, additional_months: Optional[list[float]] = None
) -> list[float]:
    """Pre-dose ADA sampling months across the comparative period.

    Dense early (when treatment-emergent ADA appear) and then quarterly, always
    including the final visit so the end-of-study incidence is measurable.

    ``additional_months`` are user-requested extra ADA draws (e.g. a site
    wants an extra check at month 9.5), merged into the SAME schedule this
    function is the single source of truth for — both build_duration_plan()
    below and services/sampling_schedule.py's own ADA-hours derivation call
    THIS function, so adding the merge here (rather than in each caller)
    keeps both in agreement, which is exactly the "two engines independently
    propose two ADA schedules" contradiction this module was written to
    prevent. A value outside [0, months] is silently dropped (it would
    otherwise extend the comparative-safety period itself, a much larger
    consequence than adding one draw) — the caller is expected to validate
    and disclose rejected values, since this function has no `assumptions`
    list of its own to report them in.
    """
    base = [0.0, 1.0, 3.0, 6.0]
    pts = [m for m in base if m <= months]
    m = 9.0
    while m <= months:
        pts.append(m)
        m += 3.0
    if float(months) not in pts:
        pts.append(float(months))
    for extra in additional_months or []:
        try:
            m_extra = float(extra)
        except (TypeError, ValueError):
            continue
        if 0.0 <= m_extra <= months:
            pts.append(m_extra)
    return sorted(set(pts))


def build_duration_plan(
    *,
    study_type: str,
    dosing: str = "single",
    subject_type: str = "healthy",
    t_half_hours: Optional[float] = None,
    t_half_basis: str = "",
    tau_hours: Optional[float] = None,
    eu_reference_product: bool = True,
    safety_months: Optional[int] = None,
    additional_immunogenicity_months: Optional[list[float]] = None,
) -> dict[str, Any]:
    """Resolve dose count to steady state and comparative-safety duration.

    Returns ``{"applicable": False, ...}`` for designs this does not govern
    (single-dose studies, non-biosimilar study types), so the caller can invoke
    it unconditionally.

    ``additional_immunogenicity_months`` are user-requested extra ADA draws,
    passed straight through to immunogenicity_schedule() (see its own
    docstring for the merge/validation rule) so this plan and
    services/sampling_schedule.py's own ADA-hours derivation — both call
    immunogenicity_schedule() — never disagree about which months are
    sampled.
    """
    is_biosimilar = str(study_type or "").strip().lower() == "biosimilar_pk"
    multiple = str(dosing or "single").strip().lower() in ("multiple", "multi", "repeat")
    is_patient = str(subject_type or "healthy").strip().lower() == "patient"

    if not is_biosimilar:
        return DurationPlan(
            applicable=False,
            reason="Comparative safety/immunogenicity duration applies to biosimilar studies only.",
        ).to_dict()
    if not multiple:
        return DurationPlan(
            applicable=False,
            reason=(
                "Single-dose study — there is no steady-state endpoint, and the comparative "
                "safety/immunogenicity period is governed by the single-dose follow-up rather "
                "than a multi-month head-to-head treatment period."
            ),
        ).to_dict()
    if not is_patient:
        # This engine's rationale (see module docstring) is specifically a
        # PATIENT staying on ongoing treatment: months-long anti-drug-antibody
        # development is why the study outlives the PK readout at all, and
        # primary_readout_note below names the first-dose + steady-state
        # co-primary pair that services/biostatistics.py's
        # co_primary_endpoints_for() only returns for subject_type=="patient".
        # A multiple-dose HEALTHY VOLUNTEER biosimilar study is a time-boxed
        # regimen with no ongoing-treatment immunogenicity rationale, so it
        # must not inherit a multi-month comparative-safety period or a
        # primary-readout note describing an endpoint set it doesn't have.
        return DurationPlan(
            applicable=False,
            reason=(
                "Multiple-dose HEALTHY VOLUNTEER study — the multi-month head-to-head "
                "comparative safety/immunogenicity period exists to characterise anti-drug "
                "antibody development under ongoing PATIENT treatment; it does not apply to a "
                "time-boxed healthy-volunteer regimen. See services/biostatistics.py's "
                "co_primary_endpoints_for() for the matching (steady-state-only) endpoint set."
            ),
        ).to_dict()

    assumptions: list[str] = []

    # --- doses to steady state ----------------------------------------
    n_doses = doses_to_steady_state(t_half_hours, tau_hours, cover_all_patients=True)
    ss_day: Optional[float] = None
    if n_doses is not None and tau_hours:
        # Dose 1 is on Day 1, so the nth dose falls (n-1) intervals later.
        ss_day = round(1.0 + (n_doses - 1) * float(tau_hours) / 24.0, 1)

    if n_doses is None:
        ss_rationale = (
            "The number of doses required to reach steady state could NOT be computed because "
            "the terminal half-life and/or the dosing interval were not available. It MUST be "
            "established from the reference product's prior (innovator) trials before the "
            "protocol is finalised — the steady-state co-primary endpoint cannot be scheduled "
            "without it."
        )
        assumptions.append(
            "Doses to steady state is unresolved (missing half-life or dosing interval); the "
            "protocol must state the value taken from the innovator's own multiple-dose data."
        )
    else:
        inflated = float(t_half_hours) * _SLOW_CLEARER_THALF_FACTOR
        ss_rationale = (
            f"Steady state is taken as {HALF_LIVES_TO_STEADY_STATE:.0f} half-lives "
            f"({100 * (1 - 2 ** -HALF_LIVES_TO_STEADY_STATE):.1f}% of steady state). The median "
            f"terminal half-life is {t_half_hours:.1f} h"
            + (f" ({t_half_basis})" if t_half_basis else "")
            + f"; to ensure steady state has been reached in ALL subjects and not merely the "
            f"median subject, it is inflated to {inflated:.1f} h to represent slow clearers. "
            f"With a dosing interval of {tau_hours:.1f} h this requires {n_doses} doses, so the "
            f"steady-state PK profile is collected over the dosing interval following dose "
            f"{n_doses}"
            + (f" (approximately Day {ss_day:.0f})." if ss_day else ".")
            + f" At least {MIN_TROUGHS_TO_DEMONSTRATE} consecutive pre-dose trough "
            f"concentrations must confirm that steady state has actually been attained before "
            f"the steady-state endpoint is read; the dose count above is a prediction from the "
            f"half-life and must be corroborated against the reference product's own "
            f"multiple-dose (innovator) data."
        )
        assumptions.append(
            f"Steady state is predicted at dose {n_doses} from a "
            f"{_SLOW_CLEARER_THALF_FACTOR:.1f}x-inflated half-life; confirm against the "
            f"innovator's published multiple-dose PK before fixing the visit schedule."
        )

    # --- comparative safety / immunogenicity duration -------------------
    if safety_months is not None:
        months = int(safety_months)
        basis = (
            f"A comparative period of {months} months was specified for this protocol. The "
            f"accepted minimum for a head-to-head biosimilar safety and immunogenicity "
            f"comparison is {SAFETY_MONTHS_FLOOR} months."
        )
        if months < SAFETY_MONTHS_FLOOR:
            assumptions.append(
                f"The specified comparative period ({months} months) is SHORTER than the "
                f"{SAFETY_MONTHS_FLOOR}-month minimum normally expected for a head-to-head "
                f"biosimilar safety and immunogenicity comparison — justify it explicitly."
            )
    elif eu_reference_product:
        months = SAFETY_MONTHS_EU
        basis = (
            f"{SAFETY_MONTHS_EU} months of head-to-head comparative safety and immunogenicity. "
            f"The accepted floor is {SAFETY_MONTHS_FLOOR} months, but this programme uses the EU "
            f"reference medicinal product as comparator and is therefore on a path where the EU "
            f"expectation of a {SAFETY_MONTHS_EU}-month comparative safety package applies; the "
            f"longer period satisfies both. A US-only filing could be shortened to "
            f"{SAFETY_MONTHS_FLOOR} months, which would be a deliberate scope decision."
        )
    else:
        months = SAFETY_MONTHS_FLOOR
        basis = (
            f"{SAFETY_MONTHS_FLOOR} months of head-to-head comparative safety and "
            f"immunogenicity — the accepted minimum. Note that an EU filing has conventionally "
            f"expected {SAFETY_MONTHS_EU} months."
        )

    # The PK readout must genuinely finish BEFORE the comparative safety period
    # ends, or "read earlier, follow up longer" (B2) is not actually true for
    # this drug. ss_day is when dose n is GIVEN; the steady-state profile then
    # runs a further dosing interval beyond it, so the profile's own END, not
    # its start, is what must fall inside the safety period. For a long-
    # half-life biologic on an infrequent dosing interval this can genuinely
    # fail (e.g. a ~52-day half-life dosed q2w), so it is checked rather than
    # asserted — the safety period is EXTENDED to clear it with a margin
    # rather than silently shortening the PK design to fit, or leaving the
    # document's own timeline internally contradictory.
    readout_end_day: Optional[float] = None
    if ss_day is not None and tau_hours:
        readout_end_day = ss_day + float(tau_hours) / 24.0
        months_days = months * 30.44
        if readout_end_day >= months_days:
            extended_months = math.ceil(readout_end_day / 30.44) + 1
            assumptions.append(
                f"The predicted steady-state PK readout (day {readout_end_day:.0f}) would fall "
                f"at or after the {months}-month comparative safety period (day "
                f"{months_days:.0f}) computed from this drug's dosing interval alone. The "
                f"comparative safety period was EXTENDED to {extended_months} months so the "
                f"primary PK analysis genuinely completes, with margin, before the study ends — "
                f"confirm this against the innovator's actual multiple-dose data before finalising."
            )
            months = extended_months

    primary_note = (
        "The PRIMARY PK ENDPOINT IS READ BEFORE THE STUDY ENDS. The co-primary first-dose and "
        "steady-state AUC(0-tau) comparison completes once the steady-state profile has been "
        f"collected, whereas subjects continue in the head-to-head comparative period to "
        f"month {months} for safety and immunogenicity. The protocol must therefore define a "
        "primary analysis timepoint distinct from the end-of-study timepoint, state that the "
        "treatment allocation is maintained between them, and pre-specify that the primary PK "
        "analysis is performed at the earlier timepoint without stopping or unblinding the "
        "ongoing safety comparison."
    )

    if additional_immunogenicity_months:
        accepted_months = [
            float(m) for m in additional_immunogenicity_months
            if isinstance(m, (int, float)) and 0.0 <= float(m) <= months
        ]
        rejected_months = [
            float(m) for m in additional_immunogenicity_months
            if not (isinstance(m, (int, float)) and 0.0 <= float(m) <= months)
        ]
        if accepted_months:
            assumptions.append(
                f"User-requested additional immunogenicity timepoint(s) added: "
                f"month(s) {sorted(accepted_months)}."
            )
        if rejected_months:
            assumptions.append(
                f"User-requested additional immunogenicity timepoint(s) "
                f"{sorted(rejected_months)} fall outside the {months}-month comparative "
                f"period and were NOT added; extending the period itself would require "
                f"a deliberate scope decision, not an incidental one."
            )

    return DurationPlan(
        applicable=True,
        t_half_hours=round(float(t_half_hours), 2) if t_half_hours else None,
        t_half_basis=t_half_basis,
        tau_hours=round(float(tau_hours), 2) if tau_hours else None,
        doses_to_steady_state=n_doses,
        steady_state_day=ss_day,
        troughs_to_demonstrate=MIN_TROUGHS_TO_DEMONSTRATE,
        steady_state_rationale=ss_rationale,
        safety_months=months,
        safety_floor_months=SAFETY_MONTHS_FLOOR,
        safety_basis=basis,
        primary_readout_note=primary_note,
        immunogenicity_timepoints_months=immunogenicity_schedule(
            months, additional_months=additional_immunogenicity_months,
        ),
        assumptions=assumptions,
    ).to_dict()


def comparative_safety_directive(plan: Optional[dict[str, Any]]) -> str:
    """Render the duration plan as LOCKED facts for the generation prompt."""
    if not plan or not plan.get("applicable"):
        return ""
    lines = [
        "\nSTUDY DURATION AND COMPARATIVE SAFETY PERIOD - ALREADY DETERMINED.",
        "Do NOT re-derive the dose count or the follow-up duration, and do NOT",
        "state that the study ends when the PK endpoint is read.",
    ]
    if plan.get("doses_to_steady_state"):
        lines.append(
            f"- DOSES TO STEADY STATE: {plan['doses_to_steady_state']}. The steady-state "
            f"AUC(0-tau) co-primary endpoint is collected over the dosing interval following "
            f"that dose"
            + (f" (approximately Day {plan['steady_state_day']:.0f})." if plan.get("steady_state_day") else ".")
        )
        lines.append(
            f"- At least {plan['troughs_to_demonstrate']} consecutive pre-dose trough "
            f"concentrations must confirm steady state has been attained before that endpoint "
            f"is read."
        )
    else:
        lines.append(
            "- DOSES TO STEADY STATE: NOT ESTABLISHED. State plainly that the number of doses "
            "required to reach steady state in all subjects must be taken from the reference "
            "product's prior multiple-dose (innovator) trials; do NOT invent a dose number."
        )
    lines.append(f"    {plan.get('steady_state_rationale', '')}")
    lines.append(
        f"- HEAD-TO-HEAD COMPARATIVE SAFETY AND IMMUNOGENICITY PERIOD: "
        f"{plan['safety_months']} months (minimum accepted: {plan['safety_floor_months']} months)."
    )
    lines.append(f"    {plan.get('safety_basis', '')}")
    if plan.get("immunogenicity_timepoints_months"):
        pts = ", ".join(
            f"month {m:g}" if m else "baseline (pre-first-dose)"
            for m in plan["immunogenicity_timepoints_months"]
        )
        lines.append(
            f"- Anti-drug antibody (ADA) sampling, pre-dose, at: {pts}. Confirmed-positive "
            "samples are titred and tested for neutralising activity. ADA incidence is compared "
            "head-to-head between arms."
        )
    lines.append(f"- {plan.get('primary_readout_note', '')}")
    for note in plan.get("assumptions", []):
        lines.append(f"- ASSUMPTION (state it explicitly): {note}")
    return "\n".join(lines) + "\n"
