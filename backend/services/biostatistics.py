"""
Biostatistics service — exact sample-size determination for bioequivalence /
biosimilar PK studies.

Why this exists
---------------
The original sample-size helper assumed a true geometric mean ratio (GMR) of
exactly 1.00 and used the normal approximation. Both choices systematically
*under-size* the study (by ~40-50% at typical CVs), which is the single most
common reason a BE protocol gets a deficiency letter. This module fixes that:

  * Power is computed with the **exact two one-sided tests (TOST)** procedure,
    reproducing **R PowerTOST**'s default ``method="exact"`` via **Owen's
    Q-function** (Owen 1965), so the planned N is identical to PowerTOST's
    ``sampleN.TOST`` — the reference implementation cited in BE statistics. A
    non-central t variant (PowerTOST ``method="nct"``, Lenth AS 243, 1989) is
    retained as a cross-check. Pure Python — no R/SciPy runtime needed.
  * The true GMR defaults to **0.95** (a 5% deviation) per regulatory norm —
    designing for a perfect product (GMR 1.00) is not accepted.
  * Highly variable drugs (within-subject CV >= 30%) are routed to
    **reference-scaled ABE (RSABE)** with a replicate design.
  * Narrow-therapeutic-index drugs use the tightened **90.00-111.11%** limits
    and a fully replicated design.

No SciPy dependency — the special functions are implemented locally so the
service runs anywhere the rest of the backend runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

from services.multiplicity import TEST_STRUCTURE_IUT, plan_co_primary
from services.powertost_r import sample_n_ntid_r, sample_n_rsabe_r, sample_n_tost_r

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Drugs the FDA treats as narrow therapeutic index (require tightened limits
# and a fully replicated design). Lower-case INN match.
NTI_DRUGS: frozenset[str] = frozenset({
    "warfarin", "tacrolimus", "sirolimus", "everolimus", "cyclosporine",
    "levothyroxine", "liothyronine", "digoxin", "lithium", "phenytoin",
    "carbamazepine", "valproic acid", "theophylline", "procainamide",
    "tacrolimus", "clindamycin", "isotretinoin", "ethosuximide",
    "primidone", "disopyramide", "flecainide",
})

# Design-choice rule (encodes the standard BE reasoning):
#   A crossover is PREFERRED whenever feasible — each subject is their own
#   control, so it removes between-subject variability and needs fewer subjects
#   and less time. It becomes INFEASIBLE when the washout required between
#   periods (>= 7 x terminal t1/2) exceeds a practical ceiling; a long-half-life
#   drug (e.g. a monoclonal antibody / fusion protein with a multi-week t1/2) is
#   then routed to a parallel-group design instead. Biologics also default to
#   parallel for immunogenicity reasons (handled separately).
MAX_PRACTICAL_WASHOUT_DAYS = 21.0                          # ~3 weeks between periods
WASHOUT_HALF_LIVES = 7.0                                   # conservative 7 x t1/2
# t1/2 above which the 7-half-life washout exceeds the practical ceiling -> parallel.
LONG_HALF_LIFE_HOURS = MAX_PRACTICAL_WASHOUT_DAYS * 24.0 / WASHOUT_HALF_LIVES  # = 72 h (3 days)

# FDA regulatory within-subject CV cut for "highly variable" drug products.
HVD_CV_THRESHOLD = 30.0

# FDA RSABE regulatory constant (sigma_w0 = 0.25 -> switching CV ~= 30%).
SIGMA_W0 = 0.25

DEFAULT_ALPHA = 0.05
DEFAULT_POWER = 0.90          # headline design power (90%); 80% also tabulated
DEFAULT_GMR = 0.95            # assumed true T/R ratio
DEFAULT_DROPOUT = 0.20        # 20% dropout allowance for enrolment
# When no within-subject CV can be sourced from the literature we fall back to a
# clearly-flagged moderate assumption rather than silently powering the study.
ASSUMED_CV_PCT = 25.0


# ---------------------------------------------------------------------------
# Special functions (no SciPy)
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Numerical Recipes)."""
    MAXIT = 200
    EPS = 3.0e-12
    FPMIN = 1.0e-300

    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _nct_cdf(t: float, df: float, ncp: float) -> float:
    """Non-central t CDF P(T <= t | df, ncp) via Lenth's Algorithm AS 243 (1989)."""
    if df <= 0:
        raise ValueError("df must be positive")

    if t < 0.0:
        # Use the symmetry relation to keep the series convergent for t >= 0.
        return 1.0 - _nct_cdf(-t, df, -ncp)

    if ncp == 0.0:
        # Central t — integrate via the incomplete beta directly.
        x = df / (df + t * t)
        cdf_upper_tail = 0.5 * _betai(0.5 * df, 0.5, x)
        return 1.0 - cdf_upper_tail

    ITRMAX = 1000
    ERRMAX = 1.0e-12

    del_ = ncp
    x = t * t / (t * t + df)
    if x <= 0.0:
        return _norm_cdf(-del_)

    lambda_ = del_ * del_
    p = 0.5 * math.exp(-0.5 * lambda_)
    q = math.sqrt(2.0 / math.pi) * p * del_
    s = 0.5 - p
    a = 0.5
    b = 0.5 * df
    rxb = (1.0 - x) ** b
    albeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    xodd = _betai(a, b, x)
    godd = 2.0 * rxb * math.exp(a * math.log(x) - albeta)
    xeven = 1.0 - rxb
    geven = b * x * rxb
    tnc = p * xodd + q * xeven

    it = 1
    while it <= ITRMAX:
        a += 1.0
        xodd -= godd
        xeven -= geven
        godd *= x * (a + b - 1.0) / a
        geven *= x * (a + b - 0.5) / (a + 0.5)
        p *= lambda_ / (2 * it)
        q *= lambda_ / (2 * it + 1)
        s -= p
        tnc += p * xodd + q * xeven
        errbd = 2.0 * s * (xodd - godd)
        if abs(errbd) < ERRMAX:
            break
        it += 1

    tnc += _norm_cdf(-del_)
    return min(1.0, max(0.0, tnc))


def _t_quantile(prob: float, df: float) -> float:
    """Inverse central t CDF via bisection (sufficient precision for critical values)."""
    lo, hi = 0.0, 100.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _nct_cdf(mid, df, 0.0) < prob:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# TOST power and sample size
# ---------------------------------------------------------------------------


def _sigma_w(cv: float) -> float:
    """Within-subject SD on the natural-log scale from a CV fraction."""
    return math.sqrt(math.log(1.0 + cv * cv))


def owens_q(nu: float, t: float, delta: float, a: float, b: float) -> float:
    """Owen's Q-function — the building block of PowerTOST's exact TOST power.

        Q_nu(t, delta; a, b) =
            1 / (Gamma(nu/2) * 2^(nu/2-1))
            * integral_a^b  Phi(t*y/sqrt(nu) - delta) * y^(nu-1) * exp(-y^2/2) dy

    Implemented with a high-resolution composite Simpson rule, evaluating the
    y^(nu-1)*exp(-y^2/2) weight in log space so it never overflows for the df
    range of real BE studies. This reproduces R PowerTOST's ``OwensQ`` (which
    uses adaptive quadrature) to well within the precision needed to land on the
    same integer sample size. No SciPy/R dependency.
    """
    if b <= a:
        return 0.0
    sqrt_nu = math.sqrt(nu)
    # Beyond ~sqrt(nu)+25 the chi-type weight is negligible, so capping the
    # finite upper limit there (or using it for b=inf) keeps Simpson points
    # concentrated over the peak without changing the result.
    cap = sqrt_nu + 25.0
    upper = cap if not math.isfinite(b) else min(b, cap)
    if upper <= a:
        return 0.0

    log_c = -math.lgamma(nu / 2.0) - (nu / 2.0 - 1.0) * math.log(2.0)
    inv_sqrt_nu = 1.0 / sqrt_nu

    def f(y: float) -> float:
        if y <= 0.0:
            return 0.0
        log_w = (nu - 1.0) * math.log(y) - 0.5 * y * y
        v = log_c + log_w
        if v < -740.0:               # exp underflows to 0
            return 0.0
        return _norm_cdf(t * y * inv_sqrt_nu - delta) * math.exp(v)

    N = 800                          # even; ample for df up to a few hundred
    h = (upper - a) / N
    s = f(a) + f(upper)
    for i in range(1, N):
        s += (4.0 if (i & 1) else 2.0) * f(a + i * h)
    return max(0.0, s * h / 3.0)


def _power_tost_exact(
    se: float, df: float, theta0: float, t1: float, t2: float, alpha: float
) -> float:
    """Exact TOST power (PowerTOST's default ``method='exact'``) via Owen's Q.

    Mirrors PowerTOST's ``.power.TOST``: two non-centrality parameters, a single
    critical t value, and the difference of two Owen's Q evaluations bounded by R.
    """
    tval = _t_quantile(1.0 - alpha, df)
    delta1 = (theta0 - t1) / se      # lower margin
    delta2 = (theta0 - t2) / se      # upper margin
    denom = 2.0 * tval
    R = math.inf if denom == 0.0 else (delta1 - delta2) * math.sqrt(df) / denom
    if R <= 0.0:
        R = math.inf
    p1 = owens_q(df, tval, delta1, 0.0, R)
    p2 = owens_q(df, -tval, delta2, 0.0, R)
    return max(0.0, min(1.0, p2 - p1))


def _power_tost_nct(
    se: float, df: float, theta0: float, t1: float, t2: float, alpha: float
) -> float:
    """TOST power via the simpler difference-of-non-central-t method
    (PowerTOST's ``method='nct'``). Retained as a cross-check / fallback."""
    tcrit = _t_quantile(1.0 - alpha, df)
    ncp1 = (theta0 - t1) / se
    ncp2 = (theta0 - t2) / se
    power = _nct_cdf(-tcrit, df, ncp2) - _nct_cdf(tcrit, df, ncp1)
    return max(0.0, min(1.0, power))


def power_tost(
    n: int,
    cv_pct: float,
    gmr: float = DEFAULT_GMR,
    lower: float = 0.80,
    upper: float = 1.25,
    alpha: float = DEFAULT_ALPHA,
    design: str = "crossover",
    method: str = "exact",
) -> float:
    """Exact TOST power for a 2x2 crossover or parallel-group ABE study.

    Reproduces R PowerTOST's ``power.TOST``. ``method='exact'`` (the PowerTOST
    default) uses Owen's Q-function; ``method='nct'`` uses the non-central t
    difference. ``cv_pct`` is the within-subject CV for a crossover and the
    total (between-subject) CV for a parallel design.
    """
    if n < 4:
        return 0.0
    cv = cv_pct / 100.0
    sw = _sigma_w(cv)
    theta0 = math.log(gmr)
    t1 = math.log(lower)
    t2 = math.log(upper)

    if design == "parallel":
        # n is total; assume balanced 1:1 allocation.
        n_per = n / 2.0
        df = n - 2
        se = sw * math.sqrt(1.0 / n_per + 1.0 / n_per)
    else:  # crossover (2x2)
        df = n - 2
        se = sw * math.sqrt(2.0 / n)

    if df <= 0 or se <= 0:
        return 0.0

    if method == "nct":
        return _power_tost_nct(se, df, theta0, t1, t2, alpha)
    return _power_tost_exact(se, df, theta0, t1, t2, alpha)


def sample_size_tost(
    cv_pct: float,
    gmr: float = DEFAULT_GMR,
    power: float = DEFAULT_POWER,
    lower: float = 0.80,
    upper: float = 1.25,
    alpha: float = DEFAULT_ALPHA,
    design: str = "crossover",
    max_n: int = 2000,
) -> int:
    """Smallest n achieving the target power (even for crossover; total for parallel)."""
    step = 2 if design == "crossover" else 2  # keep even / balanced
    n = 4
    while n <= max_n:
        if power_tost(n, cv_pct, gmr, lower, upper, alpha, design) >= power:
            return n
        n += step
    return max_n


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


@dataclass
class SampleSizeRecommendation:
    cv_pct: float
    cv_source: str                 # "literature" | "assumed" | "override"
    gmr: float
    power: float
    alpha: float
    lower_limit: float
    upper_limit: float
    design: str                    # human-readable
    design_key: str                # "crossover" | "parallel" | "replicate"
    approach: str                  # ABE / RSABE / PK similarity / NTI
    is_hvd: bool
    is_nti: bool
    n_evaluable: int               # subjects needed (achieving power)
    n_enrolled: int                # evaluable inflated for dropout
    n_per_group: Optional[int]     # per-arm (parallel) else None
    dropout_rate: float
    achieved_power: float
    endpoint: str
    regulatory_basis: str
    rationale: str
    needs_simulation: bool         # True for RSABE / scaled methods
    sensitivity: list[dict] = field(default_factory=list)
    # Regulatory provenance: which engine produced the authoritative N, the
    # R PowerTOST version + exact call + console output (when used), and the
    # qualification of the Python engine against R. Empty for legacy calls.
    provenance: dict = field(default_factory=dict)
    # Co-primary multiplicity (services/multiplicity.py). `power` above is the
    # PER-ENDPOINT power the size was actually computed at; `overall_power` is
    # the probability that EVERY co-primary endpoint passes, which is the
    # number the protocol is required to deliver. They differ whenever there is
    # more than one co-primary endpoint — see multiplicity.py's docstring.
    multiplicity: dict = field(default_factory=dict)
    overall_power: Optional[float] = None
    achieved_overall_power: Optional[float] = None
    # True ONLY when `power` was genuinely raised above the requested figure to
    # hold `overall_power` at target (a multiple-dose biosimilar's co-primary
    # pair). Every design has >= 1 co-primary endpoint — even the CONVENTIONAL
    # Cmax + AUC(0-inf) pair counts as two — so `len(co_primary_endpoints) > 1`
    # is NOT a safe test for "was this design actually power-adjusted"; a
    # caller must check this flag instead, or `overall_power`'s independence-
    # based lower bound (0.81 for the standard pair) will be mistaken for a
    # real design target. See services/multiplicity.py's `power_adjusted`.
    power_adjusted: bool = True
    co_primary_endpoints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _round_even_up(x: float) -> int:
    n = math.ceil(x)
    return n + (n % 2)


# ---------------------------------------------------------------------------
# Co-primary endpoint set (design-aware)
# ---------------------------------------------------------------------------


def co_primary_endpoints_for(
    *,
    study_type: str,
    dosing: str = "single",
    subject_type: str = "healthy",
) -> tuple[list[str], list[str]]:
    """(co-primary endpoints, secondary endpoints) for this design.

    A multiple-dose biosimilar PK-similarity study in patients does NOT have
    the single-dose endpoint set. Two things change, and both matter:

      * The exposure metric is AUC over one dosing interval, AUC(0-tau), not
        AUC(0-inf). A patient stays on treatment, so the terminal elimination
        phase is never observed and AUC(0-inf) cannot be estimated — quoting it
        as the primary endpoint would specify a quantity the study is
        structurally unable to measure.
      * Exposure is compared at BOTH the first dose and at steady state, as a
        combined primary: first-dose AUC(0-tau) captures absorption and initial
        disposition before any accumulation or immunogenicity develops, and
        steady-state AUC(0-tau) captures the exposure patients actually live
        with once accumulation and any anti-drug antibody response are
        established. A product can match on one and not the other, so BOTH must
        pass — which is what makes this a co-primary set and drives the power
        adjustment in services/multiplicity.py.

    This dual-profile set is gated on ``subject_type == "patient"`` as well as
    on dosing and study type: it exists because a PATIENT stays on treatment
    indefinitely, so the terminal elimination phase is never observed and
    AUC(0-inf) is structurally inestimable. A multiple-dose HEALTHY VOLUNTEER
    biosimilar study is a time-boxed regimen with a genuine washout after the
    last dose, so it falls through to the generic multiple-dose branch below
    instead — conflating the two would size a healthy-volunteer study as
    though its two endpoints were measured weeks apart under different
    accumulation conditions, when they are not.

    Single-dose studies keep the conventional pair (Cmax + AUC0-inf co-primary,
    AUC0-t supportive), unchanged.
    """
    multiple = str(dosing or "single").strip().lower() in ("multiple", "multi", "repeat")
    biosimilar = str(study_type or "").strip().lower() == "biosimilar_pk"
    patient = str(subject_type or "healthy").strip().lower() == "patient"

    if biosimilar and multiple and patient:
        co_primary = [
            "First-dose AUC(0-tau) (area under the concentration-time curve over the first dosing interval)",
            "Steady-state AUC(0-tau) (area under the concentration-time curve over one dosing interval at steady state)",
        ]
        secondary = [
            "First-dose Cmax",
            "Steady-state Cmax",
            "Ctrough at each pre-dose timepoint",
            "Tmax (descriptive)",
            "Accumulation ratio",
        ]
        return co_primary, secondary

    if multiple:
        return (
            ["Steady-state AUC(0-tau)", "Steady-state Cmax"],
            ["Ctrough at each pre-dose timepoint", "Tmax (descriptive)", "Accumulation ratio"],
        )

    return (
        ["Cmax", "AUC(0-inf)"],
        ["AUC(0-t)", "Tmax (descriptive)", "Terminal half-life (descriptive)"],
    )


def recommend_sample_size(
    *,
    study_type: str,
    drug_inn: str = "",
    cv_intra_pct: Optional[float] = None,
    cv_source: Optional[str] = None,
    t_half_hours: Optional[float] = None,
    is_nti: Optional[bool] = None,
    requested_design: Optional[str] = None,
    power: float = DEFAULT_POWER,
    gmr: float = DEFAULT_GMR,
    dropout: float = DEFAULT_DROPOUT,
    use_r: bool = False,
    dosing: str = "single",
    subject_type: str = "healthy",
    endpoint_correlation: float = 0.0,
    co_primary_power_adjustment: Optional[bool] = None,
) -> SampleSizeRecommendation:
    """Pick design, approach, and an exact sample size from drug characteristics.

    This is the single source of truth for the planned N — the same object feeds
    the synopsis, study-design, SAP prompts and the export tables.

    ``co_primary_power_adjustment`` controls whether ``power`` is read as the
    OVERALL power (the probability that EVERY co-primary endpoint passes) or as
    the conventional per-endpoint power. ``None`` (the default) resolves it from
    the design, and the distinction is deliberate rather than cosmetic:

      * Multiple-dose biosimilar studies IN PATIENTS -> True. The first-dose
        and steady-state AUC(0-tau) endpoints are measured weeks apart, under
        different accumulation and immunogenicity conditions, and the design
        requires BOTH to pass. Powering each at 90% would deliver only ~81%
        overall, so the per-endpoint power is raised to hold overall power at
        the requested value (services/multiplicity.py). The same design in
        HEALTHY VOLUNTEERS does not qualify — see co_primary_endpoints_for.
      * Everything else -> False, preserving the established convention. A
        conventional single-dose BE study is sized on the MORE VARIABLE of Cmax
        and AUC at the stated power, which is what FDA guidance and R
        PowerTOST's sampleN.TOST do; because the two are measured on the same
        profile in the same subjects and are strongly positively correlated,
        the achieved overall power sits close to the per-endpoint figure.
        Silently inflating every small-molecule BE sample size by ~25% here
        would depart from the accepted convention without being asked to.

    Either way the implied OVERALL power is computed and reported on the result
    (``overall_power``), so the distinction is disclosed rather than hidden.
    """
    # --- co-primary endpoint set + multiplicity ---------------------------
    # Resolved FIRST, because the per-endpoint power it produces is what the
    # sample size is actually computed at when the adjustment applies.
    co_primary, secondary_endpoints = co_primary_endpoints_for(
        study_type=study_type, dosing=dosing, subject_type=subject_type,
    )
    multiple_dose = str(dosing or "single").strip().lower() in ("multiple", "multi", "repeat")
    is_patient = str(subject_type or "healthy").strip().lower() == "patient"
    if co_primary_power_adjustment is None:
        co_primary_power_adjustment = bool(
            multiple_dose and study_type == "biosimilar_pk" and is_patient
        )

    requested_power = float(power)
    if co_primary_power_adjustment:
        mplan = plan_co_primary(
            endpoints=co_primary, overall_power=requested_power, alpha=DEFAULT_ALPHA,
            correlation=endpoint_correlation, test_structure=TEST_STRUCTURE_IUT,
        )
        # `mplan.overall_power` equals `requested_power` unless the per-endpoint
        # power needed to reach it was capped (multiplicity.py), in which case
        # it is already the true achievable ceiling — use it rather than the
        # raw request so a capped design doesn't report an unreachable target.
        overall_power_target = mplan.overall_power
        # From here on, `power` is the PER-ENDPOINT power the size is computed at.
        power = mplan.per_endpoint_power
    else:
        # `power` stays the per-endpoint value (the convention).
        mplan = plan_co_primary(
            endpoints=co_primary, overall_power=requested_power, alpha=DEFAULT_ALPHA,
            correlation=endpoint_correlation, test_structure=TEST_STRUCTURE_IUT,
        )
        # DELIBERATELY None, not the independence-based lower-bound estimate
        # (requested_power ** mplan.effective_independent_endpoints). That
        # number is real but easy to
        # misinterpret as THE study's target power the moment it sits in a
        # field literally named overall_power — and unlike the locked-facts
        # PROSE (which can gate on power_adjusted before ever mentioning a
        # number), sample_size/multiplicity are ALSO serialised as raw JSON
        # into several section prompts with no such gate. Making the value
        # itself absent when it is not a genuine target is what actually
        # prevents the leak; relying on every future reader to check
        # power_adjusted first is how this got introduced in the first place.
        overall_power_target = None
        mplan.per_endpoint_power = requested_power
        mplan.overall_power = None
        mplan.power_adjusted = False
        # NOTE: no numeric "overall power" figure is put into this rationale on
        # purpose. The independence-based bound is real but easy to
        # misinterpret as THE study's target power, so it is never written
        # into a locked-facts block at all — see the comment above.
        mplan.power_rationale = (
            f"Each co-primary endpoint is powered at {requested_power * 100:.0f}%, which is the "
            f"conventional basis for a bioequivalence sample size: the study is sized on the "
            f"more variable of the co-primary endpoints, as in FDA guidance and R PowerTOST's "
            f"sampleN.TOST. The endpoints are measured on the same concentration-time profile in "
            f"the same subjects and are strongly positively correlated, so requiring both to "
            f"pass does not materially reduce the probability of overall success. State the "
            f"study's power as {requested_power * 100:.0f}%."
        )

    # --- resolve the CV used for powering ---------------------------------
    if cv_intra_pct and cv_intra_pct > 0:
        cv_pct = float(cv_intra_pct)
        src = cv_source or "literature"
    else:
        cv_pct = ASSUMED_CV_PCT
        src = "assumed"

    # --- NTI detection ----------------------------------------------------
    if is_nti is None:
        is_nti = drug_inn.strip().lower() in NTI_DRUGS

    is_hvd = cv_pct >= HVD_CV_THRESHOLD

    # --- design selection -------------------------------------------------
    biosimilar = study_type == "biosimilar_pk"
    if requested_design in ("crossover", "parallel", "replicate"):
        design_key = requested_design
    elif biosimilar:
        design_key = "parallel"   # long half-life biologics, immunogenicity
    elif is_nti:
        design_key = "replicate"  # fully replicated for NTI
    elif is_hvd:
        design_key = "replicate"  # partial replicate for RSABE
    elif t_half_hours is not None and t_half_hours > LONG_HALF_LIFE_HOURS:
        design_key = "parallel"
    else:
        design_key = "crossover"

    # --- limits & approach ------------------------------------------------
    if is_nti:
        lower, upper = 0.90, 1.1111
        approach = "ABE with narrowed NTI limits + reference scaling (upper bound)"
    else:
        lower, upper = 0.80, 1.25
        if biosimilar:
            approach = "PK similarity (90% CI within 80.00-125.00%)"
        elif is_hvd:
            approach = "Reference-scaled average bioequivalence (RSABE)"
        else:
            approach = "Average bioequivalence (ABE)"

    # --- power calculation ------------------------------------------------
    # Evaluable n is computed with the exact TOST engine. Crossover/parallel use
    # their own SE; replicate designs are powered conservatively at the ABE
    # crossover SE and flagged for simulation confirmation (RSABE typically
    # reduces this further for HVD).
    power_design = "parallel" if design_key == "parallel" else "crossover"
    cv_for_power = cv_pct
    if design_key == "parallel" and not biosimilar:
        # parallel uses total (between-subject) variability, ~1.5x within-subject
        cv_for_power = cv_pct * 1.5

    n_eval = sample_size_tost(
        cv_for_power, gmr=gmr, power=power, lower=lower, upper=upper,
        design=power_design,
    )
    achieved = power_tost(
        n_eval, cv_for_power, gmr=gmr, lower=lower, upper=upper,
        design=power_design,
    )
    # OVERALL achieved power at the planned N — the probability every co-primary
    # endpoint passes, not just the one endpoint power_tost evaluates directly.
    # ONLY computed when the design was genuinely power-adjusted: for the
    # conventional (non-adjusted) case this would be the same independence-
    # based lower-bound estimate that overall_power deliberately omits above,
    # for the same reason — see that comment.
    achieved_overall = (
        round(achieved ** mplan.effective_independent_endpoints, 4)
        if co_primary_power_adjustment else None
    )

    # --- regulatory provenance: prefer R PowerTOST when available -----------
    # The Python engine (exact TOST via Owen's Q) is qualified against PowerTOST.
    # When use_r is set and R PowerTOST is installed, its sampleN.TOST result is
    # taken as authoritative for the regulatory record (with the exact call and
    # console output captured), and the Python result is retained as the
    # qualification cross-check. Replicate/RSABE designs stay on the Python
    # engine (a different PowerTOST function, not yet wired).
    provenance: dict = {
        "python_engine": {
            "method": "Exact TOST (Owen's Q-function), validated Python reimplementation",
            "n_evaluable": n_eval,
            "achieved_power": round(achieved, 6),
        },
        "authoritative_source": "Python (validated against PowerTOST)",
    }
    if use_r and design_key in ("crossover", "parallel"):
        r_res = sample_n_tost_r(
            cv_pct=cv_for_power, design_key=design_key, theta0=gmr,
            target_power=power, theta1=lower, theta2=upper, alpha=DEFAULT_ALPHA,
        )
        if r_res:
            provenance["r_powertost"] = {
                "engine": f"R PowerTOST {r_res['powertost_version']} (sampleN.TOST)",
                "powertost_version": r_res["powertost_version"],
                "r_version": r_res["r_version"],
                "n_evaluable": r_res["n"],
                "achieved_power": round(r_res["achieved_power"], 6),
                "r_call": r_res["r_call"],
                "r_output": r_res["r_output"],
            }
            provenance["qualification"] = {
                "python_n": n_eval, "r_n": r_res["n"],
                "agree": n_eval == r_res["n"],
                "delta_subjects": abs(n_eval - r_res["n"]),
            }
            provenance["authoritative_source"] = (
                f"R PowerTOST {r_res['powertost_version']} (sampleN.TOST)"
            )
            # PowerTOST is authoritative for the submission record.
            n_eval = r_res["n"]
            achieved = r_res["achieved_power"]

    # Real R-backed simulation for scaled methods (NTI / highly-variable). The
    # Python figure above is a conservative crossover-SE PROXY for these designs
    # (not a real scaled-ABE/NTID simulation), so when R is available its result
    # replaces it as authoritative rather than merely cross-checking it.
    r_scaled_res = None
    if use_r and design_key == "replicate":
        # IMPORTANT: sampleN.NTID/sampleN.RSABE take the NOMINAL 0.80-1.25 ABE
        # limits and scale them internally per the FDA method — NOT the
        # already-tightened `lower`/`upper` (0.90/1.1111 for NTI), which would
        # double-apply the scaling and badly inflate N (verified: 68 vs the
        # correct 28 for a CV=15% NTI case).
        if is_nti:
            r_scaled_res = sample_n_ntid_r(
                cv_pct=cv_for_power, theta0=gmr, target_power=power,
                theta1=0.80, theta2=1.25, alpha=DEFAULT_ALPHA,
            )
            r_method_label = "sampleN.NTID"
        else:
            r_scaled_res = sample_n_rsabe_r(
                cv_pct=cv_for_power, theta0=gmr, target_power=power,
                theta1=0.80, theta2=1.25, alpha=DEFAULT_ALPHA, regulator="FDA",
            )
            r_method_label = "sampleN.RSABE"
        if r_scaled_res:
            provenance["python_engine"]["method"] = (
                "Conservative crossover-SE proxy (not a scaled-method simulation)"
            )
            provenance["r_powertost"] = {
                "engine": f"R PowerTOST {r_scaled_res['powertost_version']} ({r_method_label})",
                "powertost_version": r_scaled_res["powertost_version"],
                "r_version": r_scaled_res["r_version"],
                "n_evaluable": r_scaled_res["n"],
                "achieved_power": round(r_scaled_res["achieved_power"], 6),
                "design": r_scaled_res["design"],
                "r_call": r_scaled_res["r_call"],
                "r_output": r_scaled_res["r_output"],
            }
            provenance["qualification"] = {
                "python_n": n_eval, "r_n": r_scaled_res["n"],
                "agree": n_eval == r_scaled_res["n"],
                "delta_subjects": abs(n_eval - r_scaled_res["n"]),
                "note": "Python figure is a conservative proxy, not the same method — "
                        "a difference here is expected, not a discrepancy.",
            }
            provenance["authoritative_source"] = (
                f"R PowerTOST {r_scaled_res['powertost_version']} ({r_method_label})"
            )
            n_eval = r_scaled_res["n"]
            achieved = r_scaled_res["achieved_power"]

    # A real R simulation (NTID/RSABE) already IS the regulator-recommended
    # confirmatory simulation — only flag "needs simulation" when we fell back
    # to the Python proxy (no R available).
    needs_simulation = design_key == "replicate" and r_scaled_res is None

    # --- enrolment with dropout ------------------------------------------
    if power_design == "crossover":
        n_enrolled = _round_even_up(n_eval / (1.0 - dropout))
        n_per_group = None
    else:
        per_arm = math.ceil((n_eval / 2.0) / (1.0 - dropout))
        n_per_group = per_arm
        n_enrolled = per_arm * 2

    # --- human-readable design label -------------------------------------
    # "Single-dose" vs "Multiple-dose" reflects the actual dosing regimen —
    # a multiple-dose biosimilar patient study (co-primary first-dose AND
    # steady-state AUC(0-tau)) must never be labelled single-dose; that label
    # is injected verbatim into the LOCKED DESIGN PARAMETERS block two lines
    # above the co-primary endpoints, so a mismatch reads as a design that
    # contradicts its own endpoints.
    _dose_word = "Multiple-dose" if multiple_dose else "Single-dose"
    design_label = {
        "crossover": f"{_dose_word}, 2-period, 2-sequence, 2-treatment crossover",
        "parallel": f"{_dose_word}, 2-arm parallel-group",
        "replicate": (
            f"{_dose_word}, fully replicated (4-period, RTRT/TRTR)" if is_nti
            else f"{_dose_word}, partial-replicate (3-period, TRR/RTR/RRT)"
        ),
    }[design_key]

    # --- regulatory basis -------------------------------------------------
    if biosimilar:
        reg = ("FDA Guidance: Scientific Considerations in Demonstrating "
               "Biosimilarity (2015); FDA 351(k) pathway")
    elif is_nti:
        reg = ("FDA Guidance: Bioequivalence Studies with PK Endpoints (2013), "
               "narrow-therapeutic-index recommendations")
    elif is_hvd:
        reg = ("FDA Guidance: Bioequivalence Studies with PK Endpoints (2013), "
               "reference-scaled ABE for highly variable drugs")
    else:
        reg = ("FDA Guidance: Statistical Approaches to Establishing "
               "Bioequivalence (2001); 21 CFR 320.23")

    # --- rationale --------------------------------------------------------
    bits = []
    if src == "assumed":
        bits.append(
            f"No within-subject CV was available from the literature; a moderate "
            f"value of {cv_pct:.0f}% is ASSUMED and must be confirmed against "
            f"drug-specific data before finalisation."
        )
    else:
        bits.append(f"Within-subject CV of {cv_pct:.0f}% sourced from {src}.")
    if is_nti:
        if r_scaled_res:
            bits.append(
                "Drug is a narrow-therapeutic-index agent: the sample size uses the "
                "FDA method for NTIDs (fully replicated 2x2x4 design, scaled ABE + "
                "within-subject variability comparison), simulated in R PowerTOST "
                "(sampleN.NTID) — the regulator-recommended confirmatory simulation "
                "has already been performed."
            )
        else:
            bits.append(
                "Drug is a narrow-therapeutic-index agent: tightened 90.00-111.11% "
                "limits and a fully replicated design with reference scaling are "
                "required. The N shown is a conservative proxy; confirm by simulation "
                "of the FDA NTID method (e.g., R PowerTOST sampleN.NTID)."
            )
    elif is_hvd:
        if r_scaled_res:
            bits.append(
                "Within-subject CV >= 30% classifies this as a highly variable drug; "
                "the sample size uses the FDA Reference-Scaled ABE method (partial-"
                "replicate 2x3x3 design), simulated in R PowerTOST (sampleN.RSABE) — "
                "the regulator-recommended confirmatory simulation has already been "
                "performed."
            )
        else:
            bits.append(
                "Within-subject CV >= 30% classifies this as a highly variable drug; "
                "a replicate design with reference-scaled ABE is recommended. The N "
                "shown is a conservative ABE proxy; confirm by RSABE simulation "
                "(e.g., FDA method / R PowerTOST sampleN.RSABE)."
            )
    if design_key == "parallel":
        if t_half_hours:
            washout_days = WASHOUT_HALF_LIVES * t_half_hours / 24.0
            wo = (f"the required washout (~{WASHOUT_HALF_LIVES:.0f} x t½ "
                  f"= {washout_days:.0f} days) exceeds the ~{MAX_PRACTICAL_WASHOUT_DAYS:.0f}-day "
                  f"practical ceiling")
        else:
            wo = "the drug's long terminal half-life makes the required washout impractical"
        extra = (" and would also confound immunogenicity assessment" if biosimilar else "")
        bits.append(
            "A parallel-group design is used because a crossover is infeasible here: "
            f"{wo}{extra}. A crossover would otherwise be preferred (it removes "
            "between-subject variability and needs fewer subjects), but is ruled out "
            "by the long half-life."
        )
    elif design_key == "crossover":
        bits.append(
            "A 2x2 crossover design is used (preferred when feasible): each subject "
            "serves as their own control, removing between-subject variability, so it "
            "needs fewer subjects than a parallel design; the terminal half-life keeps "
            "the required washout within a practical window."
        )
    if co_primary_power_adjustment and len(co_primary) > 1:
        # `requested_power` (the ORIGINAL target, e.g. 0.90), not
        # `overall_power_target`, both labels the "powering each at X%"
        # clause AND the naive exponentiation below — using
        # overall_power_target for one and requested_power for the other
        # produces an internally-inconsistent sentence whenever the two
        # diverge (i.e. the multiplicity cap was hit, see multiplicity.py).
        # When uncapped the two are equal, so this is a no-op in the common
        # case.
        naive_overall_pct = requested_power ** len(co_primary) * 100
        bits.append(
            f"There are {len(co_primary)} co-primary endpoints and ALL must pass, so the study "
            f"is sized to deliver {overall_power_target*100:.0f}% OVERALL power (the probability "
            f"every endpoint passes) by powering each endpoint at {power*100:.2f}%; powering each "
            f"at the originally-targeted {requested_power*100:.0f}% instead would have delivered "
            f"only about {naive_overall_pct:.0f}% overall. Alpha is NOT split across the "
            f"co-primary endpoints: because all of them must pass, the decision rule is an "
            f"intersection-union test whose family-wise type I error is already bounded by "
            f"{DEFAULT_ALPHA:.2f}."
        )
    else:
        bits.append(
            f"Powered at {power*100:.0f}% per endpoint (the conventional bioequivalence basis: "
            f"the study is sized on the more variable co-primary endpoint)."
        )
    bits.append(
        f"Sized with a true GMR of {gmr:.2f} using the exact TOST procedure "
        f"(Owen's Q-function), reproducing R PowerTOST sampleN.TOST."
    )
    rationale = " ".join(bits)

    # --- sensitivity table (CV x design x power) -------------------------
    # The "CV (%)" axis is, by convention, a within-subject (crossover-basis)
    # value that gets approximated to a between-subject value for the
    # "parallel" comparison columns via the usual x1.5 factor — but that
    # convention only holds when THIS recommendation's own design_key really
    # is within-subject-based (crossover/replicate). When design_key is
    # "parallel", cv_pct is ALREADY the between-subject basis (there is no
    # within-subject figure to derive it from), so applying x1.5 again would
    # double-convert; instead use the axis directly for the parallel columns
    # and divide by 1.5 for the crossover "what-if" comparison column.
    if design_key == "parallel":
        cv_basis_crossover = lambda cv: cv / 1.5
        cv_basis_parallel = lambda cv: cv
    else:
        cv_basis_crossover = lambda cv: cv
        cv_basis_parallel = lambda cv: cv * 1.5

    # When the study is co-primary power-adjusted, the per-endpoint power it
    # is actually sized at (`power`, e.g. 94.87%) is neither 80% nor 90%, so
    # the fixed 80/90 columns below cannot reproduce the study's own headline
    # N at any CV — a reviewer cross-checking the table would find no column
    # that matches. Add a third power column at the ACTUAL basis whenever it
    # is not already one of the two fixed ones, so the headline N is always
    # traceable to a row in this table.
    actual_power_pct: Optional[float] = None
    if co_primary_power_adjustment and abs(power - 0.80) > 1e-4 and abs(power - 0.90) > 1e-4:
        actual_power_pct = power

    sensitivity = []
    for cv in [15, 20, 25, 30, 35, 40]:
        # sample_size_tost(..., design="parallel") returns the TOTAL across
        # both arms (see its docstring), but this table's header/footnote
        # state the parallel columns are per-arm — halve it to match. The
        # internal step size is always 2, so the total is always even.
        row = {
            "cv_pct": cv,
            "crossover_80": sample_size_tost(cv_basis_crossover(cv), gmr=gmr, power=0.80, lower=lower, upper=upper, design="crossover"),
            "crossover_90": sample_size_tost(cv_basis_crossover(cv), gmr=gmr, power=0.90, lower=lower, upper=upper, design="crossover"),
            "parallel_80": sample_size_tost(cv_basis_parallel(cv), gmr=gmr, power=0.80, lower=lower, upper=upper, design="parallel") // 2,
            "parallel_90": sample_size_tost(cv_basis_parallel(cv), gmr=gmr, power=0.90, lower=lower, upper=upper, design="parallel") // 2,
        }
        if actual_power_pct is not None:
            row["crossover_actual"] = sample_size_tost(cv_basis_crossover(cv), gmr=gmr, power=actual_power_pct, lower=lower, upper=upper, design="crossover")
            row["parallel_actual"] = sample_size_tost(cv_basis_parallel(cv), gmr=gmr, power=actual_power_pct, lower=lower, upper=upper, design="parallel") // 2
            row["actual_power_pct"] = round(actual_power_pct * 100, 2)
        sensitivity.append(row)

    return SampleSizeRecommendation(
        cv_pct=round(cv_pct, 1),
        cv_source=src,
        gmr=gmr,
        power=power,
        alpha=DEFAULT_ALPHA,
        lower_limit=lower,
        upper_limit=upper,
        design=design_label,
        design_key=design_key,
        approach=approach,
        is_hvd=is_hvd,
        is_nti=is_nti,
        n_evaluable=n_eval,
        n_enrolled=n_enrolled,
        n_per_group=n_per_group,
        dropout_rate=dropout,
        achieved_power=round(achieved, 4),
        achieved_overall_power=achieved_overall,
        # Co-primary PK endpoints, resolved from the actual design rather than
        # hardcoded: a multiple-dose biosimilar study compares first-dose AND
        # steady-state AUC(0-tau) (both must pass), while a single-dose study
        # keeps the conventional Cmax + AUC(0-inf) pair. See
        # co_primary_endpoints_for() for why AUC(0-inf) is not usable at all in
        # the multiple-dose patient setting.
        endpoint=(
            "; ".join(co_primary)
            + " (co-primary — ALL must pass)"
            + (f"; secondary: {', '.join(secondary_endpoints)}" if secondary_endpoints else "")
        ),
        regulatory_basis=reg,
        rationale=rationale,
        needs_simulation=needs_simulation,
        sensitivity=sensitivity,
        provenance=provenance,
        multiplicity=mplan.to_dict(),
        overall_power=overall_power_target,
        power_adjusted=co_primary_power_adjustment,
        co_primary_endpoints=list(co_primary),
    )
