"""PK blood-sampling schedule - deterministic, Tmax- and t1/2-justified timepoints.

WHY THIS MODULE EXISTS
----------------------
Every other number that a reviewer can challenge in this product is computed in
Python and injected into the prompt as a locked fact (sample size in
:mod:`services.biostatistics`, washout in ``ai_service._locked_design_directive``,
covariate balance in :mod:`services.stratification`). The blood-draw TIMEPOINTS
were the exception: they were described in prose in the ``study_assessments``
instructions and left for the model to invent per generation. That is the one
place in a BE/PK protocol where an invented number is not merely embarrassing but
IRREVERSIBLE:

  * A sparse peak CLIPS Cmax. If the true Tmax falls between two draws, the
    observed Cmax is biased low in BOTH arms by an amount that does not cancel,
    the Cmax confidence interval widens, and no amount of re-analysis recovers
    it - the blood was never drawn. The study fails on Cmax and is repeated.
  * A short tail leaves too much of AUC extrapolated. Regulators expect
    AUC(0-t) to be at least 80% of AUC(0-inf), i.e. AUC_%extrap < 20%; a
    schedule that stops at ~2 terminal half-lives misses that by construction.
  * Too few points in the log-linear phase makes lambda_z (and therefore
    AUC(0-inf), t1/2 and CL/F) unestimable. The convention is at least three
    points in the terminal phase.

So this engine computes the schedule from the drug's ACTUAL Tmax and terminal
half-life, reports the predicted AUC_%extrap it will achieve, adds up the blood
that will actually be taken out of a healthy volunteer, and hands the model a
list it may only narrate.

WHAT IT SUPERSEDES
------------------
``services.three_stage_pipeline._default_sampling_schedule`` produced a fixed
early block plus a 1.5x geometric tail from the half-life alone. It ignored Tmax
entirely, so a drug with a 6 h Tmax (modified release, or a slowly absorbed
biologic) got its peak covered by whatever the fixed block happened to contain.
That helper is dead code behind an unused endpoint; this module replaces it.

THE DESIGN NUANCE THAT MAKES THIS CORRECT
-----------------------------------------
The schedule is not one profile - it is one profile PER PERIOD, and the design
decides how many periods a subject sits through:

  * **Crossover / replicate** - the same profile is repeated in every period, so
    the blood volume multiplies by the number of periods, and every period after
    the first needs a pre-dose sample that also serves as the CARRY-OVER check
    (FDA BE guidance: if a subject's pre-dose concentration exceeds 5% of that
    subject's Cmax, the subject's data for that period are excluded).
  * **Parallel** - one period, no washout, no carry-over check; the pre-dose
    sample is a baseline/analytical blank only.

Getting that backwards either doubles a volume that was never drawn or halves a
volume that was.

REGULATORY FRAMING
------------------
FDA "Bioequivalence Studies With Pharmacokinetic Endpoints" (Dec 2022) and
"Statistical Approaches to Establishing Bioequivalence"; EMA Guideline on the
Investigation of Bioequivalence (CPMP/EWP/QWP/1401/98 Rev.1) sections on
sampling and on AUC(0-t)/AUC(0-inf); ICH M13A (2024) sample-collection section;
FDA Bioanalytical Method Validation (May 2018) for the assay side.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

# Reused (never re-derived) from the sample-size engine so the sampling horizon
# and the washout the protocol already locks stay on the same arithmetic.
from services.biostatistics import MAX_PRACTICAL_WASHOUT_DAYS, WASHOUT_HALF_LIVES

# ---------------------------------------------------------------------------
# Regulatory / clinical constants
# ---------------------------------------------------------------------------

# AUC(0-t) must be >= 80% of AUC(0-inf) -> the extrapolated tail must stay under
# 20%. Stated in the FDA BE guidance and EMA CPMP/EWP/QWP/1401/98 Rev.1; a
# subject whose AUC_%extrap exceeds this is normally excluded from the AUC(0-inf)
# analysis, so a schedule that plans to exceed it plans to lose data.
AUC_EXTRAP_LIMIT_PCT = 20.0

# Terminal coverage targets, expressed in terminal half-lives after dosing.
# 3 x t1/2 already leaves only 12.5% extrapolated and is the regulatory floor;
# 5 x t1/2 (3.1% extrapolated) is the target when it is practically achievable.
TERMINAL_TARGET_HALF_LIVES = 5.0
TERMINAL_MIN_HALF_LIVES = 3.0

# lambda_z (terminal slope) is fitted by log-linear regression. The universal
# convention (FDA/EMA guidance, Phoenix WinNonlin default, and every NCA SOP) is
# at least THREE points in the terminal log-linear phase, and they must span a
# meaningful stretch of it - we require them inside the final two half-lives.
MIN_LAMBDA_Z_POINTS = 3
# A drug with a declared bi-exponential/two-compartment disposition needs one
# more, because the first post-peak points belong to the distribution phase and
# are not eligible for the terminal fit.
MIN_LAMBDA_Z_POINTS_MULTICOMPARTMENT = 4
LAMBDA_Z_SPAN_HALF_LIVES = 2.0
# Floor on how much of the profile counts as the terminal window, as a fraction
# of the whole sampling period. For an ordinary drug the horizon is ~5 x t1/2,
# so two half-lives is already 40% of it and this floor never binds. It binds
# only when Tmax dominates the horizon (flip-flop / modified-release kinetics),
# where "the final two half-lives" can be a window shorter than the clock grid
# and the terminal slope is in practice read off the latter part of the tail.
MIN_TERMINAL_WINDOW_FRACTION = 0.25

# Blood volume. 4 mL of whole blood yields ~1.8-2.0 mL of plasma, enough for a
# duplicate LC-MS/MS injection plus an incurred-sample-reanalysis aliquot and a
# retention aliquot. The reference protocol template quotes 4-6 mL per PK draw;
# the conservative end is used so the volume total is not optimistic.
PK_DRAW_VOLUME_ML = 4.0
# Anti-drug-antibody / neutralising-antibody draws need a larger serum volume
# because the tiered assay (screen -> confirm -> NAb) is run sequentially.
ADA_DRAW_VOLUME_ML = 5.0
# Screening panel, per-period check-in panel and end-of-study panel
# (haematology, chemistry, serology, urinalysis) outside the PK draws.
SAFETY_LAB_VOLUME_ML = 30.0

# Safe blood volume for a healthy research volunteer. The NIH Clinical Center
# limit for healthy adults is 550 mL drawn in any 8-week period (and no more
# than 10.5 mL/kg), which is itself anchored on the ~450-500 mL whole-blood
# donation permitted at 8-week intervals. A 500 mL planning ceiling is used
# here: it is inside the NIH limit, it matches the figure the protocol template
# already quotes, and it leaves headroom for repeat/unscheduled draws.
MAX_BLOOD_VOLUME_8WEEK_ML = 500.0
MAX_BLOOD_VOLUME_ML_PER_KG_8WEEK = 10.5
# Lightest subject a conventional BE protocol admits (BMI 18.5 at ~1.65 m).
# The per-kg limit is evaluated at this weight, i.e. for the worst case.
MIN_SUBJECT_WEIGHT_KG = 50.0
EIGHT_WEEKS_HOURS = 8.0 * 7.0 * 24.0

# FDA BE guidance carry-over rule: a pre-dose concentration above 5% of that
# subject's Cmax for the period means the washout failed for that subject and
# the period's data are excluded. Only meaningful in a multi-period design.
PREDOSE_CARRYOVER_LIMIT_PCT = 5.0

# Practical ceilings on how long a subject can realistically be followed.
# Small molecules reuse the sample-size engine's 21-day practical ceiling so the
# two engines cannot disagree about what "practical" means. Biologic PK
# similarity studies routinely run to Day 85-90; beyond that dropout and
# treatment-emergent ADA erode the terminal phase faster than the extra
# timepoints improve it.
MAX_SAMPLING_HOURS_SMALL_MOLECULE = MAX_PRACTICAL_WASHOUT_DAYS * 24.0   # 504 h
MAX_SAMPLING_HOURS_BIOLOGIC = 90.0 * 24.0                              # 2160 h

# Fallback PK values, used ONLY when the literature gave us nothing. Each one is
# reported in `assumptions` and raises a must-disclose flag - the protocol text
# has to say the schedule rests on an assumed value, exactly as
# biostatistics.ASSUMED_CV_PCT is disclosed for sample size.
ASSUMED_T_HALF_HOURS_SMALL_MOLECULE = 24.0      # mirrors three_stage_pipeline
ASSUMED_T_HALF_HOURS_BIOLOGIC = 336.0           # 14 d, typical IgG1 mAb
ASSUMED_TMAX_HOURS_ORAL = 2.0                   # typical immediate-release oral
ASSUMED_TMAX_HOURS_BIOLOGIC = 96.0              # 4 d, mid-range for SC mAbs
# Steady-state confirmation: trough (pre-dose) concentrations on three
# consecutive days before the profile day, per FDA multiple-dose BE practice.
STEADY_STATE_TROUGH_DAYS = 3
DEFAULT_DOSING_INTERVAL_HOURS = 24.0            # once daily, when tau is unknown

# ---------------------------------------------------------------------------
# Schedule geometry
# ---------------------------------------------------------------------------
# Times are laid out as fractions of Tmax, not as a fixed clock grid, so a drug
# with a 20-minute Tmax and a drug with a 5-day Tmax both get the SAME shape of
# peak coverage. Absorption fractions divide the pre-peak interval; the peak
# cluster brackets Tmax on both sides so a shifted true Tmax is still captured.

_ABSORPTION_FRACTIONS: tuple[float, ...] = (0.125, 0.25, 0.375)
# 3 points below Tmax, Tmax itself, 3 points above -> the peak cannot be clipped
# by a Tmax that lands up to 2x later or 2x earlier than the literature value.
_PEAK_FRACTIONS: tuple[float, ...] = (0.5, 0.67, 0.83, 1.0, 1.25, 1.5, 2.0)
# Used when absorption is delayed or variable (modified release, fed conditions,
# food-effect study): the peak window is stretched later because the ACTUAL Tmax
# may sit well beyond the fasting literature value.
_PEAK_FRACTIONS_WIDE: tuple[float, ...] = (0.4, 0.6, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)

# Elimination sampling is GEOMETRIC (log-spaced), not linear: concentrations
# fall exponentially, so equal time steps waste draws on the early tail while
# equal ratios keep the log-linear regression evenly weighted across decades.
_ELIMINATION_RATIO = 1.5
_MIN_FIRST_DRAW_HOURS = 5.0 / 60.0     # 5 min - the fastest a site can realistically draw

# Clock grid the nominal times are snapped to, so every time is one a nurse can
# actually schedule. (upper bound in hours, grid in hours)
_TIME_GRID: tuple[tuple[float, float], ...] = (
    (2.0, 5.0 / 60.0),      # < 2 h      -> 5-minute grid
    (12.0, 15.0 / 60.0),    # 2-12 h     -> 15-minute grid
    (48.0, 1.0),            # 12-48 h    -> whole hours
    (168.0, 4.0),           # 2-7 days   -> 4-hour blocks
)
_TIME_GRID_COARSE = 24.0    # beyond 7 days -> whole study days

# Acceptable deviation from the nominal time, in minutes. Tight while the
# subject is confined and the curve is steep; wide once they are ambulatory and
# the curve is log-linear (a 2 h deviation at Day 30 costs nothing).
_WINDOW_TIERS: tuple[tuple[float, int], ...] = (
    (1.0, 2),
    (4.0, 5),
    (12.0, 10),
    (24.0, 30),
    (72.0, 60),
    (336.0, 120),
)
_WINDOW_DEFAULT_MINUTES = 1440          # +/- 1 day for weekly/monthly visits
_PREDOSE_WINDOW_MINUTES = 60            # within 60 min before dosing

# ADA/immunogenicity sampling for a biosimilar PK study: pre-dose plus the
# conventional Day 15 / 29 / 57 / 85 follow-up points, which straddle the
# 2-4 week window in which treatment-emergent ADA typically appear.
_ADA_TIMEPOINTS_HOURS: tuple[float, ...] = (0.0, 336.0, 672.0, 1344.0, 2016.0)


# ---------------------------------------------------------------------------
# Schedule-modifier catalogue
# ---------------------------------------------------------------------------
# Each entry is a label-detectable PK feature that changes what the schedule has
# to do. `effect` is the concrete change this engine makes - it is rendered into
# the protocol, so it must describe something the timepoint list actually does.


@dataclass(frozen=True)
class SamplingModifier:
    key: str
    label: str
    effect: str               # what the schedule does differently
    rationale: str            # WHY, mechanistically
    evidence: str             # what the applicability judgement rests on
    applies_when: str         # the condition that makes it relevant
    # Populated per-study by compute_sampling_schedule():
    applies: bool = False
    detected_from_label: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_CATALOGUE: tuple[SamplingModifier, ...] = (
    SamplingModifier(
        key="modified_release",
        label="Modified-release formulation (extended / delayed / enteric-coated)",
        effect=(
            "The peak cluster is widened to 0.4-3.0 x Tmax so a lag time or a "
            "flattened, later peak is still bracketed on both sides."
        ),
        rationale=(
            "A modified-release product releases drug over hours rather than "
            "minutes, so Tmax is later, flatter and far more variable between "
            "subjects than the immediate-release value. A peak cluster sized for "
            "an immediate-release profile can sit entirely on the ascending limb "
            "for one subject and entirely on the descending limb for another, "
            "which biases Cmax in opposite directions and inflates its CV."
        ),
        evidence="Dosage form stated in the label (dosage and administration / how supplied)",
        applies_when="The product is extended, delayed, sustained or controlled release",
    ),
    SamplingModifier(
        key="food_effect",
        label="Food-delayed absorption",
        effect=(
            "The peak cluster is widened to 0.4-3.0 x Tmax so the fed-condition "
            "peak, which occurs later than the fasting literature Tmax, is covered "
            "by the same timepoint list in both treatment periods."
        ),
        rationale=(
            "A high-fat meal slows gastric emptying and commonly shifts Tmax by "
            "one to several hours. A food-effect study compares fed against "
            "fasted using ONE sampling schedule, so that schedule has to bracket "
            "both peaks; sizing it on the fasting Tmax clips the fed Cmax and "
            "manufactures a food effect that is really a sampling artefact."
        ),
        evidence="Food-effect statements in the label, or a food-effect study design",
        applies_when="Food delays or alters absorption, or the study type is food_effect",
    ),
    SamplingModifier(
        key="secondary_peak",
        label="Enterohepatic recirculation / secondary absorption peak",
        effect=(
            "Additional mid-phase draws are inserted at approximately 2.5 x and "
            "3.5 x Tmax so a second peak is not read as assay noise on a curve "
            "that is otherwise only sampled log-spaced by then."
        ),
        rationale=(
            "Drugs undergoing enterohepatic recirculation (or dual-site "
            "absorption) show a distinct second concentration rise hours after "
            "the first peak. Geometric tail spacing is deliberately sparse in "
            "exactly that window, so the rise is missed, the terminal fit is "
            "contaminated by a re-absorption phase, and lambda_z is biased "
            "shallow - which inflates AUC(0-inf) and t1/2."
        ),
        evidence="Enterohepatic recirculation / double-peak statements in the label",
        applies_when="The label describes recirculation, a second peak, or biphasic absorption",
    ),
    SamplingModifier(
        key="multi_compartment",
        label="Bi-exponential (multi-compartment) disposition",
        effect=(
            "A fourth point is required inside the terminal window, because the "
            "first post-peak points belong to the distribution phase and are not "
            "eligible for the lambda_z fit."
        ),
        rationale=(
            "For a drug with a distinct distribution phase the early post-peak "
            "decline is steeper than the true terminal slope. Fitting lambda_z "
            "across that boundary over-estimates the elimination rate and "
            "under-estimates the extrapolated AUC, which is the failure mode the "
            "AUC_%extrap check is supposed to catch."
        ),
        evidence="Biphasic/two-compartment/distribution-phase statements in the label",
        applies_when="The label describes biexponential decline or a distribution phase",
    ),
    SamplingModifier(
        key="iv_infusion",
        label="Intravenous infusion administration",
        effect=(
            "The Tmax draw is designated the END-OF-INFUSION sample and must be "
            "taken from the contralateral arm; there is no absorption phase to "
            "characterise, so the pre-peak points serve as infusion-rate checks."
        ),
        rationale=(
            "For an IV infusion the maximum concentration occurs at the end of "
            "infusion by definition, not at a pharmacokinetic Tmax. Sampling from "
            "the infusion arm contaminates the sample with residual drug in the "
            "line and produces impossibly high concentrations."
        ),
        evidence="Intravenous infusion route stated in the label",
        applies_when="The product is given as an intravenous infusion",
    ),
    SamplingModifier(
        key="active_metabolite",
        label="Pharmacologically active metabolite",
        effect=(
            "The same timepoints are assayed for the active metabolite as a "
            "supportive analyte; no additional draws are required because the "
            "metabolite is measured in the aliquot already taken."
        ),
        rationale=(
            "Where an active metabolite contributes to effect, regulators expect "
            "it to be measured, but bioequivalence is still decided on the parent "
            "drug because the parent is the more sensitive indicator of "
            "formulation differences in absorption."
        ),
        evidence="Active-metabolite statements in the label",
        applies_when="The label identifies a pharmacologically active metabolite",
    ),
    SamplingModifier(
        key="immunogenicity",
        label="Immunogenicity (anti-drug antibodies)",
        effect=(
            "A separate ADA/NAb sampling series (pre-dose, Day 15, 29, 57, 85) is "
            "scheduled alongside the PK draws and its serum volume is included in "
            "the blood-volume total."
        ),
        rationale=(
            "Treatment-emergent anti-drug antibodies alter clearance and can "
            "truncate the terminal phase mid-study, so a PK similarity comparison "
            "is uninterpretable without a concurrent ADA time course. The 2-4 "
            "week window is when they typically first appear."
        ),
        evidence="Immunogenicity / anti-drug-antibody statements in the label; biologic product",
        applies_when="The product is a biologic and immunogenicity is characterised",
    ),
    SamplingModifier(
        key="flip_flop",
        label="Absorption-rate-limited (flip-flop) kinetics",
        effect=(
            "The terminal window is anchored on the observed terminal phase and "
            "the protocol must state that the apparent t1/2 may reflect absorption "
            "rather than elimination."
        ),
        rationale=(
            "When absorption is slower than elimination the terminal slope is the "
            "absorption rate constant, so the half-life used to size the sampling "
            "horizon is not the elimination half-life. Reporting the apparent "
            "value without that caveat mis-states what AUC(0-inf) extrapolates."
        ),
        evidence="Flip-flop / absorption-rate-limited statements in the label",
        applies_when="Absorption is slower than elimination",
    ),
)

_BY_KEY = {m.key: m for m in _CATALOGUE}


# ---------------------------------------------------------------------------
# Applicability detection - deterministic cues from the real label text
# ---------------------------------------------------------------------------
# Matched case-insensitively against the DailyMed label sections already fetched
# for the protocol (services/literature_service.py::_SPL_SECTIONS). Same
# approach as stratification.py and design_decisions.py: the applicability
# judgement is grounded in the product's own label, not in model recall.

_CUES: dict[str, tuple[str, ...]] = {
    "modified_release": (
        r"extended[- ]release", r"delayed[- ]release", r"controlled[- ]release",
        r"sustained[- ]release", r"modified[- ]release", r"prolonged[- ]release",
        r"enteric[- ]coated", r"gastro[- ]?resistant",
    ),
    "food_effect": (
        r"high[- ]fat meal", r"\bfed (?:state|conditions?)\b",
        r"food (?:delay|decreas|increas|reduc|slow)",
        r"(?:with|without) (?:regard to )?food",
        r"delays? (?:the )?(?:median )?t\s?max", r"administered with a meal",
    ),
    "secondary_peak": (
        r"enterohepatic", r"second(?:ary)? peak", r"double[- ]peak",
        r"biphasic absorption", r"re-?absorption",
    ),
    "multi_compartment": (
        r"bi-?exponential", r"two[- ]compartment", r"biphasic (?:decline|disposition)",
        r"distribution phase", r"alpha (?:and beta )?phase",
    ),
    "iv_infusion": (
        r"intravenous infusion", r"\biv infusion\b", r"infused over",
        r"administered (?:as|by) (?:an )?infusion",
    ),
    "active_metabolite": (
        r"active metabolite", r"pharmacologically active metabolite",
        r"equipotent metabolite",
    ),
    "immunogenicity": (
        r"anti-?drug antibod", r"\bada\b", r"immunogenic", r"neutrali[sz]ing antibod",
        r"anti-?\w+ antibodies",
    ),
    "flip_flop": (
        r"flip[- ]flop", r"absorption[- ]rate[- ]limited",
        r"absorption is (?:the )?rate[- ]limiting",
    ),
}

# Modifiers that only make sense for a biologic, whatever the label text says
# (an oral small molecule does not get an ADA assay).
_BIOLOGIC_ONLY = frozenset({"immunogenicity"})


def _label_text(label: Optional[dict[str, str]]) -> str:
    """Concatenate the label sections that carry absorption/disposition prose."""
    if not label:
        return ""
    return " ".join(
        (label or {}).get(k, "") or ""
        for k in (
            "pharmacokinetics", "clinical_pharmacology", "dosage_and_administration",
            "how_supplied", "indications_usage", "title",
        )
    )


def _detect(text: str) -> set[str]:
    """Which catalogue modifiers the label text gives positive evidence for."""
    if not text:
        return set()
    lowered = text.lower()
    hits: set[str] = set()
    for key, patterns in _CUES.items():
        if any(re.search(p, lowered) for p in patterns):
            hits.add(key)
    return hits


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _grid_for(t: float) -> float:
    """The clock grid a draw at time `t` is scheduled on."""
    for upper, g in _TIME_GRID:
        if t <= upper:
            return g
    return _TIME_GRID_COARSE


def _snap(t: float) -> float:
    """Snap a raw time to a clinically schedulable clock grid (half-up)."""
    if t <= 0:
        return 0.0
    grid = _grid_for(t)
    # math.floor(x + 0.5) rather than round(), because round() is banker's
    # rounding and would send 0.375 h down to 20 min instead of up to 25 min.
    return round(math.floor(t / grid + 0.5) * grid, 4)


def _snap_floor(t: float) -> float:
    """Snap DOWN onto the grid (used for the lower edge of the peak window)."""
    if t <= 0:
        return 0.0
    grid = _grid_for(t)
    return round(math.floor(t / grid + 1e-9) * grid, 4)


def _snap_ceil(t: float) -> float:
    """Snap UP onto the grid (used for the upper edge of the peak window)."""
    if t <= 0:
        return 0.0
    grid = _grid_for(t)
    return round(math.ceil(t / grid - 1e-9) * grid, 4)


def _snap_horizon(t: float, cap: float) -> float:
    """Snap the LAST timepoint onto the grid without ever SHORTENING the tail.

    The ordinary half-up snap is fine for an interior draw, but applied to the
    horizon it can pull the final sample earlier than the computed target - a
    200 h target lands on Day 8 (192 h) once the grid is whole days. A shorter
    tail means a LARGER extrapolated share of AUC than the protocol has already
    promised, and it is the one draw that cannot be repeated. The horizon is
    therefore snapped UP, then clipped back to the practical follow-up ceiling
    (itself a whole number of days, so the clip only bites where the horizon was
    already truncated and declared).
    """
    if t <= 0:
        return 0.0
    up = _snap_ceil(t)
    return min(up, _snap(cap)) if cap and cap > 0 else up


def _fmt_hours(t: float) -> str:
    """Compact hour string: 0.5 -> '0.5', 24.0 -> '24'."""
    r = round(t, 3)
    return f"{r:g}"


def _day_number(t: float) -> int:
    """Study day, with Day 1 = the dosing day (t = 0)."""
    return int(t // 24.0) + 1


def _clock_label(t: float) -> str:
    """Human-readable draw time. Long biologic schedules are expressed in study
    days, because 'Day 29 (672 h)' is what a site can actually book."""
    if t <= 0:
        return "Pre-dose (0 h)"
    if t < 1.0:
        return f"{int(round(t * 60))} min"
    if t < 24.0:
        return f"{_fmt_hours(t)} h"
    return f"Day {_day_number(t)} ({_fmt_hours(t)} h)"


def _window_minutes(t: float) -> int:
    if t <= 0:
        return _PREDOSE_WINDOW_MINUTES
    for upper, minutes in _WINDOW_TIERS:
        if t <= upper:
            return minutes
    return _WINDOW_DEFAULT_MINUTES


def _window_label(t: float) -> str:
    if t <= 0:
        return f"within {_PREDOSE_WINDOW_MINUTES} min before dosing"
    m = _window_minutes(t)
    if m < 60:
        return f"+/- {m} min"
    if m < 1440:
        return f"+/- {m // 60} h"
    return f"+/- {m // 1440} day"


def _dedupe(times: list[float]) -> list[float]:
    """Sorted, strictly increasing, snapped times (a snapped duplicate is a real
    duplicate - the site cannot draw the same subject twice at 10 minutes)."""
    out: list[float] = []
    for t in sorted(times):
        if not out or t - out[-1] > 1e-6:
            out.append(t)
    return out


def _geometric_span(start: float, stop: float, ratio: float) -> list[float]:
    """Log-spaced points in (start, stop], with a common ratio at or below
    `ratio`. Log-spaced because concentration decays exponentially: equal time
    ratios keep the terminal regression evenly weighted instead of piling draws
    into the first hour of the tail."""
    if stop <= start * 1.02 or start <= 0:
        return []
    n = max(1, math.ceil(math.log(stop / start) / math.log(ratio)))
    r = (stop / start) ** (1.0 / n)
    return [start * (r ** k) for k in range(1, n + 1)]


def _lambda_z_window_start(t_last: float, t_half: float) -> float:
    """Start of the window a terminal-slope point has to fall in."""
    span = max(LAMBDA_Z_SPAN_HALF_LIVES * t_half,
               MIN_TERMINAL_WINDOW_FRACTION * t_last)
    return max(0.0, t_last - span)


def _ensure_terminal_density(
    times: list[float], t_last: float, t_half: float, min_points: int,
) -> list[float]:
    """Guarantee `min_points` draws inside the terminal window, by geometric
    bisection of the widest ratio gap in that window.

    Without this, a long geometric tail can end with only two widely separated
    points, and lambda_z (hence AUC(0-inf), t1/2, CL/F) is not estimable at all.
    The window start itself is used as the bisection anchor so an inserted point
    always lands INSIDE the window rather than off the front of it.
    """
    window_start = _lambda_z_window_start(t_last, t_half)
    if window_start >= t_last - 1e-9:
        return times
    work = list(times)
    for _ in range(3 * min_points):  # bounded; bisection converges well before
        inside = [t for t in work if t >= window_start - 1e-9]
        if len(inside) >= min_points:
            break
        candidates = _dedupe([window_start] + inside)
        widest_ratio, pair = 0.0, None
        for a, b in zip(candidates, candidates[1:]):
            if a <= 0:
                continue
            if b / a > widest_ratio:
                widest_ratio, pair = b / a, (a, b)
        if not pair:
            break
        mid = _snap(math.sqrt(pair[0] * pair[1]))
        if mid <= pair[0] + 1e-9 or mid >= pair[1] - 1e-9 or mid < window_start:
            break
        grown = _dedupe(work + [mid])
        if len(grown) == len(work):
            break
        work = grown
    return work


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_sampling_schedule(
    t_half_hours: Optional[float] = None,
    tmax_hours: Optional[float] = None,
    design_key: Optional[str] = None,
    study_type: str = "BE",
    dosing: str = "single",
    is_biologic: bool = False,
    *,
    label: Optional[dict[str, str]] = None,
    tau_hours: Optional[float] = None,
    draw_volume_ml: float = PK_DRAW_VOLUME_ML,
) -> dict[str, Any]:
    """Compute THE blood-sampling schedule for this study.

    Parameters
    ----------
    t_half_hours:
        Terminal half-life from the literature/label. Sets the sampling horizon
        and therefore the predicted AUC_%extrap. If missing, a clearly-flagged
        modality default is used.
    tmax_hours:
        Literature Tmax. Sets where the dense peak cluster goes. If missing, the
        result says so (``tmax_known`` False, a must-disclose flag, and an entry
        in ``assumptions``) rather than pretending the peak is characterised.
    design_key:
        'crossover' | 'parallel' | 'replicate'. Decides how many periods the
        profile is repeated over (blood volume) and whether the pre-dose sample
        also serves as the carry-over check.
    study_type:
        'BE' | 'food_effect' | 'biosimilar_pk'. A food-effect study always gets
        the widened peak cluster; a biosimilar PK study also gets the ADA series.
    dosing:
        'single' | 'multiple'. Multiple-dose profiles cover ONE dosing interval
        and are summarised by AUC(0-tau), for which extrapolation does not apply.
    is_biologic:
        Switches the modality defaults (longer assumed t1/2 and Tmax, 90-day
        practical ceiling, day-scale clock labels).
    label:
        Trimmed DailyMed label dict, as assembled for the protocol. Its text
        drives the modifier detection (modified release, food effect,
        enterohepatic recirculation, ...).
    tau_hours:
        Dosing interval for a multiple-dose study; defaults to 24 h with a flag.
    draw_volume_ml:
        Whole-blood volume per PK draw; defaults to PK_DRAW_VOLUME_ML.

    Returns
    -------
    A JSON-serialisable dict (it is persisted inside ``population_config``,
    a JSONB column) with the timepoint list, the AUC_%extrap prediction, the
    blood-volume audit, the detected modifiers, the assumptions relied on, and
    the flags the protocol text must surface.
    """
    design = (design_key or "crossover").strip().lower()
    study = (study_type or "BE").strip()
    multiple_dose = (dosing or "single").strip().lower() in ("multiple", "multi", "repeat")
    biologic = bool(is_biologic) or study == "biosimilar_pk"

    assumptions: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []

    # -- PK inputs, with honest fallbacks ----------------------------------
    t_half_known = bool(t_half_hours) and float(t_half_hours) > 0
    if t_half_known:
        t_half = float(t_half_hours)
        t_half_basis = "literature"
    else:
        t_half = (ASSUMED_T_HALF_HOURS_BIOLOGIC if biologic
                  else ASSUMED_T_HALF_HOURS_SMALL_MOLECULE)
        t_half_basis = "assumed"
        assumptions.append({
            "key": "t_half_hours",
            "value": t_half,
            "basis": ("Typical IgG1 monoclonal antibody terminal half-life (14 days)"
                      if biologic else
                      "Conservative small-molecule default (24 h) - same value the "
                      "pipeline uses when no half-life can be extracted"),
        })
        flags.append({
            "key": "t_half_assumed",
            "severity": "warning",
            "message": (
                f"No terminal half-life could be sourced; the sampling horizon "
                f"rests on an ASSUMED t1/2 of {_fmt_hours(t_half)} h. The protocol "
                f"must state this and the schedule must be re-confirmed against "
                f"the reference product's PK before first dosing."
            ),
            "must_surface_in_protocol": True,
        })

    tmax_known = bool(tmax_hours) and float(tmax_hours) > 0
    if tmax_known:
        tmax = float(tmax_hours)
        tmax_basis = "literature"
    else:
        # Bounded by the half-life so a fast-clearing drug is not given a peak
        # cluster that sits beyond its entire measurable profile.
        raw = ASSUMED_TMAX_HOURS_BIOLOGIC if biologic else ASSUMED_TMAX_HOURS_ORAL
        tmax = max(_MIN_FIRST_DRAW_HOURS * 4.0, min(raw, t_half))
        tmax_basis = "assumed"
        assumptions.append({
            "key": "tmax_hours",
            "value": round(tmax, 3),
            "basis": (
                f"No Tmax could be sourced. Assumed "
                f"{'4 days (mid-range for a subcutaneous monoclonal antibody)' if biologic else '2 h (typical immediate-release oral product)'}, "
                f"capped at the terminal half-life so the peak cluster stays inside "
                f"the measurable profile."
            ),
        })
        flags.append({
            "key": "tmax_assumed",
            "severity": "critical",
            "message": (
                "Tmax is NOT known for this product, so the dense peak cluster is "
                "placed on an assumed value. Cmax may be clipped if the true Tmax "
                "differs materially. The protocol must disclose this, and the "
                "schedule must be confirmed (or a pilot/lead-in cohort run) before "
                "the pivotal study is dosed."
            ),
            "must_surface_in_protocol": True,
        })

    # -- Modifiers, grounded in the real label -----------------------------
    detected = _detect(_label_text(label))
    if not biologic:
        detected -= _BIOLOGIC_ONLY
    applicable = set(detected)
    # A food-effect study widens the peak window by DESIGN, not by label cue:
    # the same schedule must bracket both the fasted and the fed peak.
    if study == "food_effect":
        applicable.add("food_effect")
    # A biosimilar PK study always carries an ADA series, whether or not the
    # reference label happens to use the word "immunogenicity".
    if study == "biosimilar_pk":
        applicable.add("immunogenicity")

    modifiers = [
        SamplingModifier(**{**asdict(m),
                            "applies": m.key in applicable,
                            "detected_from_label": m.key in detected}).to_dict()
        for m in _CATALOGUE
    ]
    applied = [m for m in modifiers if m["applies"]]

    widen_peak = bool(applicable & {"modified_release", "food_effect"})
    peak_fractions = _PEAK_FRACTIONS_WIDE if widen_peak else _PEAK_FRACTIONS
    min_lambda_z = (MIN_LAMBDA_Z_POINTS_MULTICOMPARTMENT
                    if "multi_compartment" in applicable else MIN_LAMBDA_Z_POINTS)

    # -- Sampling horizon --------------------------------------------------
    peak_end_raw = max(peak_fractions) * tmax
    practical_cap = (MAX_SAMPLING_HOURS_BIOLOGIC if biologic
                     else MAX_SAMPLING_HOURS_SMALL_MOLECULE)

    if multiple_dose:
        tau = float(tau_hours) if tau_hours and float(tau_hours) > 0 else DEFAULT_DOSING_INTERVAL_HOURS
        if not tau_hours:
            assumptions.append({
                "key": "tau_hours",
                "value": tau,
                "basis": "Dosing interval not supplied; once-daily (24 h) assumed.",
            })
            flags.append({
                "key": "tau_assumed",
                "severity": "warning",
                "message": ("The dosing interval was not supplied; a 24 h interval is "
                            "assumed for the steady-state profile and must be confirmed "
                            "against the labelled regimen."),
                "must_surface_in_protocol": True,
            })
        # tau is a fact about the regimen, not a scheduling choice, so the
        # profile ends exactly at the end of the dosing interval.
        t_last = _snap(tau)
        horizon_basis = (
            f"One complete dosing interval (tau = {_fmt_hours(tau)} h) at steady state"
        )
        truncated = False
    else:
        tau = None
        target = TERMINAL_TARGET_HALF_LIVES * t_half
        # The tail must also outlast the peak window plus enough room for the
        # terminal fit - matters for flip-flop / modified-release drugs where
        # 2-3 x Tmax can exceed 5 x t1/2.
        target = max(target, peak_end_raw + LAMBDA_Z_SPAN_HALF_LIVES * t_half)
        # Snapped UP (never down) so the delivered horizon is at least the
        # computed target and the predicted AUC_%extrap is met or bettered.
        t_last = _snap_horizon(min(target, practical_cap), practical_cap)
        truncated = target > practical_cap + 1e-9
        horizon_basis = (
            f"{TERMINAL_TARGET_HALF_LIVES:.0f} x terminal t1/2 "
            f"({_fmt_hours(t_half)} h)"
        )
        if truncated:
            horizon_basis = (
                f"Truncated at the practical follow-up ceiling of "
                f"{_fmt_hours(practical_cap / 24.0)} days "
                f"({TERMINAL_TARGET_HALF_LIVES:.0f} x t1/2 would require "
                f"{_fmt_hours(TERMINAL_TARGET_HALF_LIVES * t_half / 24.0)} days)"
            )

    # -- Build the timepoints ---------------------------------------------
    raw_times: list[float] = [0.0]
    raw_times += [f * tmax for f in _ABSORPTION_FRACTIONS]
    raw_times += [f * tmax for f in peak_fractions]
    if "secondary_peak" in applicable:
        raw_times += [2.5 * tmax, 3.5 * tmax]

    peak_start_raw = min(peak_fractions) * tmax
    # Nothing below 5 minutes: a site cannot draw faster than that post-dose.
    raw_times = [t for t in raw_times if t == 0.0 or t >= _MIN_FIRST_DRAW_HOURS]
    raw_times = [t for t in raw_times if t <= t_last + 1e-9]

    elimination_start = min(max(peak_end_raw, tmax), t_last)
    raw_times += _geometric_span(elimination_start, t_last, _ELIMINATION_RATIO)

    times = _dedupe([_snap(t) for t in raw_times])
    if times and times[-1] < t_last - 1e-9:
        times = _dedupe(times + [t_last])
    times = _ensure_terminal_density(times, t_last, t_half, min_lambda_z)

    # -- Classify, window, and cost each draw ------------------------------
    # The peak window edges are snapped OUTWARD - lower edge down, upper edge up
    # - so grid rounding can only ever WIDEN the window the bracketing count is
    # taken over, never narrow it. Narrowing it is how a legitimate
    # ascending-limb draw gets reclassified out of the peak and the cluster then
    # LOOKS under-bracketed when it is not: for a 15-minute Tmax the nominal
    # 0.5 x Tmax edge is 7.5 min, which a half-up snap pushes to 10 min - past
    # the 5-minute draw that genuinely brackets the rise.
    peak_start = _snap_floor(peak_start_raw)
    peak_end = min(_snap_ceil(peak_end_raw), t_last)
    tmax_snapped = _snap(tmax)
    lambda_z_window_start = _lambda_z_window_start(t_last, t_half)

    timepoints: list[dict[str, Any]] = []
    cumulative = 0.0
    for i, t in enumerate(times, 1):
        if t == 0.0:
            phase = "pre-dose"
            purpose = (
                "Baseline / analytical blank; in every period after the first it is "
                "also the carry-over check."
                if design in ("crossover", "replicate") else
                "Baseline / analytical blank."
            )
        elif t < peak_start - 1e-9:
            phase = "absorption"
            purpose = "Characterises the absorption limb so the rise to Cmax is defined."
        elif t <= peak_end + 1e-9:
            phase = "peak"
            if abs(t - tmax_snapped) < 1e-9:
                purpose = (
                    "End-of-infusion sample (draw from the contralateral arm)."
                    if "iv_infusion" in applicable else
                    "Literature Tmax - the anchor of the peak cluster."
                )
            else:
                purpose = "Brackets Tmax so Cmax is not clipped by a shifted true peak."
        else:
            phase = "elimination"
            purpose = "Log-spaced elimination sample for the terminal slope."
        lambda_z_point = (t >= lambda_z_window_start - 1e-9) and t > 0.0
        cumulative += draw_volume_ml
        timepoints.append({
            "index": i,
            "hours": round(t, 4),
            "day": _day_number(t),
            "label": _clock_label(t),
            "phase": phase,
            "lambda_z_point": bool(lambda_z_point),
            "window_minutes": _window_minutes(t),
            "window": _window_label(t),
            "volume_ml": round(draw_volume_ml, 2),
            "cumulative_volume_ml": round(cumulative, 2),
            "purpose": purpose,
        })

    phase_counts = {
        p: sum(1 for tp in timepoints if tp["phase"] == p)
        for p in ("pre-dose", "absorption", "peak", "elimination")
    }
    # Bracketing is counted INSIDE the peak window only. A draw eight hours down
    # the elimination tail is "after Tmax" arithmetically but contributes
    # nothing to resolving the peak, and counting it would let a clipped Cmax
    # pass the check.
    points_before_tmax = sum(
        1 for t in times if peak_start - 1e-9 <= t < tmax_snapped - 1e-9)
    points_after_tmax = sum(
        1 for t in times if tmax_snapped + 1e-9 < t <= peak_end + 1e-9)
    terminal_points = sum(1 for tp in timepoints if tp["lambda_z_point"])
    lambda_z_estimable = terminal_points >= min_lambda_z
    # At steady state the profile covers ONE dosing interval and the exposure
    # metric is AUC(0-tau). The terminal slope is not fitted from an intra-tau
    # profile at all (it needs the post-final-dose decline), so a steady-state
    # profile that cannot support lambda_z is expected, not a defect - flagging
    # it as critical would be noise that trains a reviewer to ignore the flags.
    lambda_z_required = not multiple_dose

    if lambda_z_required and not lambda_z_estimable:
        flags.append({
            "key": "lambda_z_not_estimable",
            "severity": "critical",
            "message": (
                f"Only {terminal_points} sample(s) fall at or after "
                f"{_fmt_hours(lambda_z_window_start)} h (the terminal window); at "
                f"least {min_lambda_z} are needed to fit lambda_z, without which "
                f"AUC(0-inf), t1/2 and CL/F cannot be reported."
            ),
            "must_surface_in_protocol": True,
        })
    # A steady-state profile runs for exactly one dosing interval, so a Tmax at
    # or beyond tau cannot be bracketed inside it - and, more importantly, it
    # says the regimen and the PK disagree: at steady state Tmax must fall
    # INSIDE the interval. Named explicitly, because "Cmax can be clipped"
    # points the reader at the schedule when the regimen is what is wrong.
    if multiple_dose and tau and tmax >= tau - 1e-9:
        flags.append({
            "key": "tmax_beyond_dosing_interval",
            "severity": "critical",
            "message": (
                f"The literature Tmax ({_fmt_hours(tmax)} h) is at or beyond the "
                f"dosing interval (tau = {_fmt_hours(tau)} h), so a steady-state "
                f"profile over one interval cannot characterise the peak. Confirm "
                f"the regimen and the Tmax: at steady state Tmax must fall inside "
                f"tau, otherwise a single-dose profile (or a longer interval) is "
                f"the correct design."
            ),
            "must_surface_in_protocol": True,
        })
    if points_before_tmax < 2 or points_after_tmax < 2:
        flags.append({
            "key": "peak_under_bracketed",
            "severity": "critical",
            "message": (
                f"Only {points_before_tmax} sample(s) before and "
                f"{points_after_tmax} after Tmax. Cmax can be clipped, and a "
                f"clipped Cmax cannot be recovered by re-analysis."
            ),
            "must_surface_in_protocol": True,
        })

    # -- AUC extrapolation prediction --------------------------------------
    lambda_z = math.log(2.0) / t_half
    if multiple_dose:
        auc_metric = "AUC(0-tau)"
        auc_extrap_pct: Optional[float] = None
        auc_extrap_ok = True
        auc_note = (
            "At steady state the exposure metric is AUC over one dosing interval, "
            "so no extrapolation to infinity is performed and AUC_%extrap does not "
            "apply."
        )
    else:
        auc_metric = "AUC(0-t) and AUC(0-inf)"
        # First-order elimination from a one-compartment approximation: the AUC
        # left beyond t_last is C_last/lambda_z, which is exp(-lambda_z * t_last)
        # of the total. Exact for a mono-exponential decline; for a drug with a
        # deep distribution phase the true extrapolated share can be larger,
        # which is why the multi_compartment modifier exists.
        auc_extrap_pct = round(100.0 * math.exp(-lambda_z * t_last), 2)
        auc_extrap_ok = auc_extrap_pct <= AUC_EXTRAP_LIMIT_PCT
        auc_note = (
            f"Predicted from first-order elimination: exp(-lambda_z x t_last) with "
            f"lambda_z = ln2/t1/2 = {lambda_z:.5f} /h and t_last = "
            f"{_fmt_hours(t_last)} h. Mono-exponential approximation; confirm "
            f"against the observed profile."
        )
        if not auc_extrap_ok:
            flags.append({
                "key": "auc_extrap_exceeds_limit",
                "severity": "critical",
                "message": (
                    f"Predicted AUC_%extrap is {auc_extrap_pct:.1f}%, above the "
                    f"{AUC_EXTRAP_LIMIT_PCT:.0f}% regulatory expectation, because "
                    f"the schedule can only reach "
                    f"{t_last / t_half:.1f} x t1/2 within the practical follow-up "
                    f"ceiling. State AUC(0-t) as the primary AUC metric with "
                    f"AUC(0-inf) supportive, and pre-specify the handling of "
                    f"subjects whose observed extrapolation exceeds "
                    f"{AUC_EXTRAP_LIMIT_PCT:.0f}%."
                ),
                "must_surface_in_protocol": True,
            })
        elif t_last < TERMINAL_MIN_HALF_LIVES * t_half - 1e-9:
            flags.append({
                "key": "terminal_coverage_below_target",
                "severity": "warning",
                "message": (
                    f"Sampling reaches {t_last / t_half:.1f} x t1/2, short of the "
                    f"{TERMINAL_MIN_HALF_LIVES:.0f} x t1/2 convention, though the "
                    f"predicted extrapolation ({auc_extrap_pct:.1f}%) still meets "
                    f"the {AUC_EXTRAP_LIMIT_PCT:.0f}% expectation."
                ),
                "must_surface_in_protocol": True,
            })
    if truncated:
        flags.append({
            "key": "horizon_truncated",
            "severity": "warning",
            "message": (
                f"The full {TERMINAL_TARGET_HALF_LIVES:.0f} x t1/2 horizon would run "
                f"to {_fmt_hours(TERMINAL_TARGET_HALF_LIVES * t_half / 24.0)} days; "
                f"sampling is truncated at the practical follow-up ceiling of "
                f"{_fmt_hours(practical_cap / 24.0)} days."
            ),
            "must_surface_in_protocol": True,
        })

    # -- Design-dependent extras ------------------------------------------
    periods = _periods_for(design)
    if design in ("crossover", "replicate"):
        predose_rule = (
            f"In every period the pre-dose sample is the carry-over check: if a "
            f"subject's pre-dose concentration exceeds "
            f"{PREDOSE_CARRYOVER_LIMIT_PCT:.0f}% of that subject's Cmax for the "
            f"period, the subject's data for that period are excluded from the "
            f"bioequivalence analysis."
        )
    else:
        predose_rule = None

    steady_state_troughs: list[dict[str, Any]] = []
    if multiple_dose:
        # Trough samples on consecutive pre-dose days demonstrate that steady
        # state was reached before the profile day is run.
        for k in range(STEADY_STATE_TROUGH_DAYS, 0, -1):
            steady_state_troughs.append({
                "label": f"Pre-dose trough, {k} day(s) before the profile day",
                "window": f"within {_PREDOSE_WINDOW_MINUTES} min before that day's dose",
                "volume_ml": round(draw_volume_ml, 2),
                "purpose": ("Confirms attainment of steady state before the "
                            "profile-day PK sampling."),
            })

    immunogenicity_timepoints: list[dict[str, Any]] = []
    if "immunogenicity" in applicable:
        for t in _ADA_TIMEPOINTS_HOURS:
            immunogenicity_timepoints.append({
                "hours": round(t, 2),
                "day": _day_number(t),
                "label": _clock_label(t),
                "window": _window_label(t),
                "volume_ml": ADA_DRAW_VOLUME_ML,
                "assay": ("Tiered ADA testing: screening bridging ELISA/ECL -> "
                          "confirmatory competitive inhibition -> NAb assay if "
                          "confirmed positive."),
            })

    analytes = ["Parent drug (primary)"]
    if "active_metabolite" in applicable:
        analytes.append("Active metabolite (supportive; bioequivalence is decided "
                        "on the parent drug)")

    # -- Blood volume audit -------------------------------------------------
    blood = _blood_volume_audit(
        times=times,
        periods=periods,
        draw_volume_ml=draw_volume_ml,
        t_half=t_half,
        t_last=t_last,
        design=design,
        ada_draws=len(immunogenicity_timepoints),
        trough_draws=len(steady_state_troughs),
    )
    if not blood["within_limit"]:
        flags.append({
            "key": "blood_volume_exceeds_limit",
            "severity": "critical",
            "message": (
                f"The planned draws reach {blood['max_8_week_ml']:.0f} mL in an "
                f"8-week window, above the {blood['limit_ml']:.0f} mL healthy-"
                f"volunteer ceiling. Reduce the per-draw volume to "
                f"{blood['compliant_draw_volume_ml']} mL (or use a micro-sampling "
                f"assay) before the protocol is finalised."
            ),
            "must_surface_in_protocol": True,
        })

    result: dict[str, Any] = {
        "timepoints": timepoints,
        "n_timepoints": len(timepoints),
        "phase_counts": phase_counts,
        "peak_bracketing": {
            "tmax_hours": round(tmax_snapped, 4),
            "points_before_tmax": points_before_tmax,
            "points_after_tmax": points_after_tmax,
            "peak_window_hours": [round(peak_start, 4), round(peak_end, 4)],
        },
        "tmax_hours": round(tmax, 4),
        "tmax_known": tmax_known,
        "tmax_basis": tmax_basis,
        "t_half_hours": round(t_half, 4),
        "t_half_known": t_half_known,
        "t_half_basis": t_half_basis,
        "lambda_z_per_hour": round(lambda_z, 6),
        "last_timepoint_hours": round(t_last, 4),
        "last_timepoint_label": _clock_label(t_last),
        "last_timepoint_half_lives": round(t_last / t_half, 2),
        "horizon_basis": horizon_basis,
        "horizon_truncated": truncated,
        "auc_metric": auc_metric,
        "auc_extrap_pct": auc_extrap_pct,
        "auc_extrap_limit_pct": AUC_EXTRAP_LIMIT_PCT,
        "auc_extrap_within_limit": auc_extrap_ok,
        "auc_extrap_basis": auc_note,
        "terminal_points": terminal_points,
        "min_terminal_points_required": min_lambda_z,
        "lambda_z_estimable": lambda_z_estimable,
        "lambda_z_required": lambda_z_required,
        "lambda_z_window_start_hours": round(lambda_z_window_start, 4),
        "design_key": design,
        "periods": periods,
        "study_type": study,
        "dosing": "multiple" if multiple_dose else "single",
        "dosing_interval_hours": round(tau, 4) if tau else None,
        "is_biologic": biologic,
        "analytes": analytes,
        "predose_carryover_rule": predose_rule,
        "steady_state_troughs": steady_state_troughs,
        "immunogenicity_timepoints": immunogenicity_timepoints,
        "blood_volume": blood,
        "modifiers": applied,
        "considered": modifiers,
        "assumptions": assumptions,
        "flags": flags,
        "must_disclose": any(f.get("must_surface_in_protocol") for f in flags),
    }
    result["summary"] = _summary(result)
    return result


def _periods_for(design: str) -> int:
    """Number of dosing periods each subject completes.

    A fully replicated design is 4-period; a partial replicate is 3-period. The
    conservative 4 is used for the blood-volume audit because under-counting
    periods under-counts the blood actually taken out of a subject.
    """
    if design == "parallel":
        return 1
    if design == "replicate":
        return 4
    return 2


def _blood_volume_audit(
    *,
    times: list[float],
    periods: int,
    draw_volume_ml: float,
    t_half: float,
    t_last: float,
    design: str,
    ada_draws: int,
    trough_draws: int,
) -> dict[str, Any]:
    """Total and worst-8-week blood volume, against the healthy-volunteer limit.

    The 8-week window is what the limit is actually written against, and in a
    crossover two whole periods can land inside one window - which is precisely
    the case a per-period total would miss. Period p starts one washout
    (7 x t1/2, the same figure the protocol already locks) plus one full profile
    after period p-1.
    """
    per_period_pk = len(times) * draw_volume_ml
    ada_ml = ada_draws * ADA_DRAW_VOLUME_ML
    trough_ml = trough_draws * draw_volume_ml * periods
    total = per_period_pk * periods + ada_ml + trough_ml + SAFETY_LAB_VOLUME_ML

    washout_hours = 0.0 if design == "parallel" else WASHOUT_HALF_LIVES * t_half
    cycle = t_last + washout_hours

    # Every draw on one study timeline, then the worst rolling 8-week window.
    draws: list[tuple[float, float]] = []
    for p in range(periods):
        offset = p * cycle
        for t in times:
            draws.append((offset + t, draw_volume_ml))
        for _ in range(trough_draws):
            draws.append((offset, draw_volume_ml))
    for i in range(ada_draws):
        draws.append((_ADA_TIMEPOINTS_HOURS[i], ADA_DRAW_VOLUME_ML))
    draws.sort()

    worst = 0.0
    for start, _ in draws:
        window = sum(v for t, v in draws if start <= t <= start + EIGHT_WEEKS_HOURS)
        worst = max(worst, window)
    # Safety labs are charged to every window - conservative by design.
    worst += SAFETY_LAB_VOLUME_ML

    per_kg_limit = MAX_BLOOD_VOLUME_ML_PER_KG_8WEEK * MIN_SUBJECT_WEIGHT_KG
    limit = min(MAX_BLOOD_VOLUME_8WEEK_ML, per_kg_limit)

    max_draws_in_window = max(
        (sum(1 for t, _ in draws if s <= t <= s + EIGHT_WEEKS_HOURS) for s, _ in draws),
        default=0,
    )
    compliant_draw_volume = draw_volume_ml
    if worst > limit and max_draws_in_window:
        allowance = (limit - SAFETY_LAB_VOLUME_ML - ada_ml) / max_draws_in_window
        # Round DOWN to the nearest 0.5 mL - a draw volume that only just fits
        # after rounding up would not fit.
        compliant_draw_volume = max(0.5, math.floor(allowance * 2.0) / 2.0)

    return {
        "draw_volume_ml": round(draw_volume_ml, 2),
        "draws_per_period": len(times),
        "periods": periods,
        "pk_volume_ml": round(per_period_pk * periods, 1),
        "ada_volume_ml": round(ada_ml, 1),
        "steady_state_trough_volume_ml": round(trough_ml, 1),
        "safety_lab_volume_ml": round(SAFETY_LAB_VOLUME_ML, 1),
        "total_ml": round(total, 1),
        "max_8_week_ml": round(worst, 1),
        "limit_ml": round(limit, 1),
        "limit_basis": (
            f"{MAX_BLOOD_VOLUME_8WEEK_ML:.0f} mL in any 8-week window (planning "
            f"ceiling inside the NIH Clinical Center 550 mL / 8-week healthy-adult "
            f"limit), also checked against "
            f"{MAX_BLOOD_VOLUME_ML_PER_KG_8WEEK} mL/kg at the lightest permitted "
            f"subject weight ({MIN_SUBJECT_WEIGHT_KG:.0f} kg = "
            f"{per_kg_limit:.0f} mL)."
        ),
        "within_limit": worst <= limit,
        "compliant_draw_volume_ml": round(compliant_draw_volume, 2),
    }


def _summary(r: dict[str, Any]) -> str:
    """One-paragraph plain statement for the protocol narrative."""
    n = r["n_timepoints"]
    pb = r["peak_bracketing"]
    parts = [
        f"{n} pharmacokinetic samples per period are drawn over "
        f"{r['last_timepoint_label']}, comprising a pre-dose baseline, "
        f"{r['phase_counts']['absorption']} absorption-phase samples, "
        f"{r['phase_counts']['peak']} samples clustered around a Tmax of "
        f"{_fmt_hours(pb['tmax_hours'])} h "
        f"({pb['points_before_tmax']} before and {pb['points_after_tmax']} after it), "
        f"and {r['phase_counts']['elimination']} log-spaced elimination samples."
    ]
    if r["auc_extrap_pct"] is None:
        parts.append(
            f"The exposure metric is {r['auc_metric']} over one dosing interval, so "
            f"no extrapolation to infinity is required."
        )
    else:
        parts.append(
            f"The last sample at {_fmt_hours(r['last_timepoint_hours'])} h is "
            f"{r['last_timepoint_half_lives']} terminal half-lives post-dose, giving "
            f"a predicted AUC extrapolation of {r['auc_extrap_pct']:.1f}% "
            f"({'within' if r['auc_extrap_within_limit'] else 'ABOVE'} the "
            f"{AUC_EXTRAP_LIMIT_PCT:.0f}% expectation), with "
            f"{r['terminal_points']} samples available for the lambda_z fit."
        )
    bv = r["blood_volume"]
    parts.append(
        f"Across {bv['periods']} period(s) the total volume drawn is "
        f"{bv['total_ml']:.0f} mL ({bv['max_8_week_ml']:.0f} mL in the worst 8-week "
        f"window against a {bv['limit_ml']:.0f} mL limit)."
    )
    if r["tmax_basis"] == "assumed":
        parts.append(
            "Tmax was NOT available from the literature; the peak cluster rests on "
            "an assumed value that the protocol must disclose."
        )
    return " ".join(parts)


def sampling_schedule_directive(result: dict[str, Any]) -> str:
    """Render the locked timepoint list as prompt text for the generator.

    Mirrors ``stratification.stratification_directive`` and
    ``design_decisions.design_directive``: the model narrates these facts and
    may not add, drop, round or re-derive a single time.
    """
    if not result or not result.get("timepoints"):
        return ""
    tp = result["timepoints"]
    pb = result.get("peak_bracketing", {})
    lines = [
        "PK BLOOD-SAMPLING SCHEDULE - these timepoints are ALREADY DETERMINED. "
        "Reproduce this EXACT list of nominal times; do not add, drop, round, "
        "re-order or re-derive them, and do not invent a different total blood "
        "volume.",
        f"Basis: Tmax = {_fmt_hours(pb.get('tmax_hours', 0))} h "
        f"({result.get('tmax_basis')}), terminal t1/2 = "
        f"{_fmt_hours(result.get('t_half_hours', 0))} h "
        f"({result.get('t_half_basis')}); horizon = {result.get('horizon_basis')}.",
        f"Nominal times (h post-dose), each repeated in EVERY one of the "
        f"{result.get('periods')} period(s):",
    ]
    for t in tp:
        lines.append(
            f"  {t['index']}. {t['hours']} h [{t['label']}] - {t['phase']} phase, "
            f"window {t['window']}, {t['volume_ml']} mL. {t['purpose']}"
        )
    bv = result.get("blood_volume", {})
    lines.append(
        f"Blood volume: {bv.get('draw_volume_ml')} mL per PK draw; "
        f"{bv.get('draws_per_period')} draws per period x {bv.get('periods')} "
        f"period(s); total {bv.get('total_ml')} mL, worst 8-week window "
        f"{bv.get('max_8_week_ml')} mL against a limit of {bv.get('limit_ml')} mL "
        f"({bv.get('limit_basis')})."
    )
    if result.get("auc_extrap_pct") is None:
        lines.append(f"Exposure metric: {result.get('auc_metric')}. "
                     f"{result.get('auc_extrap_basis')}")
    else:
        lines.append(
            f"Exposure metric: {result.get('auc_metric')}. Last sample at "
            f"{_fmt_hours(result.get('last_timepoint_hours', 0))} h = "
            f"{result.get('last_timepoint_half_lives')} x t1/2, predicted "
            f"AUC_%extrap {result.get('auc_extrap_pct')}% against a "
            f"{AUC_EXTRAP_LIMIT_PCT:.0f}% limit. {result.get('auc_extrap_basis')}"
        )
    if result.get("lambda_z_required") is False:
        lines.append(
            "lambda_z: not fitted from this profile - a steady-state profile "
            "covers one dosing interval and is summarised by AUC(0-tau); the "
            "terminal slope requires the post-final-dose decline."
        )
    else:
        lines.append(
            f"lambda_z: {result.get('terminal_points')} samples fall at or after "
            f"{_fmt_hours(result.get('lambda_z_window_start_hours', 0))} h (the "
            f"terminal window - the final {LAMBDA_Z_SPAN_HALF_LIVES:.0f} terminal "
            f"half-lives, or the last quarter of the profile if that is longer); "
            f"at least {result.get('min_terminal_points_required')} are required "
            f"for the terminal-slope fit."
        )
    if result.get("predose_carryover_rule"):
        lines.append(f"Carry-over: {result['predose_carryover_rule']}")
    for analyte in result.get("analytes", []):
        lines.append(f"Analyte: {analyte}")
    if result.get("steady_state_troughs"):
        lines.append("Steady-state confirmation troughs: " + "; ".join(
            s["label"] for s in result["steady_state_troughs"]))
    if result.get("immunogenicity_timepoints"):
        lines.append("Immunogenicity (ADA/NAb) samples: " + "; ".join(
            f"{s['label']} ({s['volume_ml']} mL)"
            for s in result["immunogenicity_timepoints"]))
    for m in result.get("modifiers", []):
        lines.append(f"PK feature - {m['label']}: {m['effect']} ({m['rationale']})")
    for a in result.get("assumptions", []):
        lines.append(f"ASSUMPTION to disclose - {a['key']} = {a['value']}: {a['basis']}")
    for f in result.get("flags", []):
        if f.get("must_surface_in_protocol"):
            lines.append(f"MUST STATE IN THE PROTOCOL ({f['severity']}): {f['message']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# QC re-verification helper
# ---------------------------------------------------------------------------
# Used by services/protocol_qc.py to check that the GENERATED text actually
# contains the schedule this engine computed. Deliberately returns plain
# strings ("pass" / "warning" / "fail") that match QCStatus values rather than
# importing protocol_qc, so the QC layer can import this module without a cycle.

# A reported time counts as matching a locked time when it is within 5% of it,
# with a 0.05 h (3 min) floor so early draws are not failed by table rounding.
_MATCH_TOLERANCE_FRACTION = 0.05
_MATCH_TOLERANCE_FLOOR_HOURS = 0.05
# Fraction of the locked schedule that may be missing before it is a hard fail
# rather than a warning.
_MISSING_FAIL_FRACTION = 0.25
# The reported last sample may not fall short of the locked one by more than
# this, or the AUC extrapolation the protocol promises is not achievable.
_LAST_POINT_TOLERANCE_FRACTION = 0.10

_TABLE_ROW_RE = re.compile(r"^\s*\|(?P<first>[^|]*)\|", re.MULTILINE)
_BARE_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:h\b|hours?\b|hr\b)", re.IGNORECASE)
_DAY_RE = re.compile(r"\bday\s*(\d+)", re.IGNORECASE)


def _parse_time_cell(cell: str) -> Optional[float]:
    """Hours from a table cell: '0.5', '0.5 h', 'Pre-dose', 'Day 8 (168)'."""
    s = (cell or "").strip().lower()
    if not s:
        return None
    if "pre-dose" in s or "predose" in s or "pre dose" in s:
        return 0.0
    # A cell that LEADS with a day number is a study day (Day 1 = dosing day),
    # not an hour - checked first so "Day 8 (168 h)" is not read as 8 hours.
    m = _DAY_RE.search(s)
    if m and s.startswith("day"):
        return (int(m.group(1)) - 1) * 24.0
    m = re.match(r"^[^\d\-]*(\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1))
    return None


def extract_reported_timepoints(text: str) -> list[float]:
    """Best-effort extraction of the sampling times a generated section states.

    Prefers the first column of a Markdown table (the format the sampling
    section is instructed to produce); falls back to bare '<n> h' mentions in
    prose. Conservative: a value it cannot confidently read is left out, which
    shows up as a missing timepoint rather than a false match.
    """
    if not text:
        return []
    cells = [_parse_time_cell(m.group("first")) for m in _TABLE_ROW_RE.finditer(text)]
    times = [c for c in cells if c is not None]
    if len(times) >= 4:
        return _dedupe(times)
    prose = [float(m.group(1)) for m in _BARE_HOURS_RE.finditer(text)]
    return _dedupe(times + prose)


def verify_sampling_schedule(
    section_text: str,
    schedule: dict[str, Any],
) -> dict[str, Any]:
    """Re-verify a generated protocol's stated timepoints against the computed
    schedule.

    Returns a dict with ``status`` ("pass" | "warning" | "fail" - the string
    values of QCStatus), the matched/missing/extra times, and a ``detail``
    sentence written for a reviewer. The hard failures are the two that cannot
    be fixed after dosing: a peak that is not bracketed (Cmax clipped) and a
    tail that stops short of the locked horizon (AUC extrapolation blown).
    """
    if not schedule or not schedule.get("timepoints"):
        return {
            "status": "not_applicable",
            "detail": "No computed sampling schedule was available to check against.",
            "reported": [], "missing": [], "extra": [], "matched": [],
        }

    expected = [tp["hours"] for tp in schedule["timepoints"]]
    reported = extract_reported_timepoints(section_text)
    if not reported:
        return {
            "status": "warning",
            "detail": ("No sampling timepoints could be parsed from the generated "
                       "section, so the locked schedule could not be verified. The "
                       "schedule must be presented as a table of nominal times."),
            "reported": [], "missing": expected, "extra": [], "matched": [],
        }

    def _tol(t: float) -> float:
        return max(_MATCH_TOLERANCE_FLOOR_HOURS, abs(t) * _MATCH_TOLERANCE_FRACTION)

    matched, missing = [], []
    for e in expected:
        if any(abs(r - e) <= _tol(e) for r in reported):
            matched.append(e)
        else:
            missing.append(e)
    extra = [r for r in reported
             if not any(abs(r - e) <= _tol(e) for e in expected)]

    tmax = (schedule.get("peak_bracketing") or {}).get("tmax_hours") or 0.0
    peak_lo, peak_hi = ((schedule.get("peak_bracketing") or {})
                        .get("peak_window_hours") or [0.0, 0.0])
    before = sum(1 for r in reported if peak_lo - _tol(peak_lo) <= r < tmax - 1e-9)
    after = sum(1 for r in reported if tmax + 1e-9 < r <= peak_hi + _tol(peak_hi))
    reported_last = max(reported)
    expected_last = schedule.get("last_timepoint_hours") or max(expected)
    last_ok = reported_last >= expected_last * (1.0 - _LAST_POINT_TOLERANCE_FRACTION)
    predose_present = any(abs(r) <= _MATCH_TOLERANCE_FLOOR_HOURS for r in reported)

    problems: list[str] = []
    status = "pass"
    if before < 2 or after < 2:
        status = "fail"
        problems.append(
            f"the stated schedule brackets Tmax ({_fmt_hours(tmax)} h) with only "
            f"{before} sample(s) before and {after} after it, so Cmax can be clipped"
        )
    if not last_ok:
        status = "fail"
        problems.append(
            f"the last stated sample ({_fmt_hours(reported_last)} h) falls short of "
            f"the locked {_fmt_hours(expected_last)} h, so the planned AUC "
            f"extrapolation of "
            f"{schedule.get('auc_extrap_pct')}% would not be achieved"
        )
    if missing and len(missing) > max(1, int(len(expected) * _MISSING_FAIL_FRACTION)):
        status = "fail"
        problems.append(
            f"{len(missing)} of {len(expected)} locked timepoints are absent from "
            f"the generated text"
        )
    if status == "pass":
        if not predose_present:
            status = "warning"
            problems.append("no pre-dose (0 h) sample is stated")
        elif missing:
            status = "warning"
            problems.append(
                f"{len(missing)} locked timepoint(s) are not stated: "
                + ", ".join(_fmt_hours(m) for m in missing[:6])
            )

    detail = (
        f"{len(matched)} of {len(expected)} locked timepoints appear in the "
        f"generated schedule."
        if not problems else
        "Sampling schedule check: " + "; ".join(problems) + "."
    )
    return {
        "status": status,
        "detail": detail,
        "reported": reported,
        "matched": matched,
        "missing": missing,
        "extra": extra,
        "predose_present": predose_present,
        "points_before_tmax": before,
        "points_after_tmax": after,
        "reported_last_hours": reported_last,
        "expected_last_hours": expected_last,
    }
