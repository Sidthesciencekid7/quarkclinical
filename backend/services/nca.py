"""
services/nca.py — Non-compartmental analysis (NCA) engine.

Computes the standard PK exposure metrics (Cmax, Tmax, AUC0-t, AUC0-inf,
lambda_z, t-half, %AUCextrap) from a concentration-time profile, using the
conventions of the reference software (Certara Phoenix WinNonlin / Phoenix NCA)
so the numbers are defensible in a bioequivalence submission.

PURE PYTHON — standard library ``math`` only. No numpy / scipy / pandas.

===========================================================================
Conventions (read this before trusting a number)
===========================================================================

BLQ (below limit of quantification) handling — ``blq_rule="std"``
----------------------------------------------------------------
A sample is "measurable" iff its concentration is not ``None``, is >= ``lloq``,
and is > 0.  A ``None`` (or a numeric value below ``lloq``) is BLQ.

  * Leading  BLQ (before the first measurable sample) -> set to 0 and KEPT.
    Their (t, 0) points contribute real trapezoids into the first rise.
  * Embedded BLQ (between two measurable samples) and Trailing BLQ (after the
    last measurable sample) -> treated as MISSING and DROPPED.  They are NOT
    set to zero.  An interval that straddles a dropped embedded BLQ is
    integrated directly between the two surrounding measurable points, and
    AUC0-t simply ends at the last measurable time (Tlast).
  * A flag is added when any embedded BLQ (and, separately, any trailing BLQ)
    was dropped.

AUC0-t — linear-up / log-down trapezoidal  (``auc0t_method``)
-------------------------------------------------------------
Summed over consecutive RETAINED points from the first available time to
Tlast (the last measurable time).  For an interval (t1,c1) -> (t2,c2):

    if  c2 >= c1  or  c1 <= 0  or  c2 <= 0:      # rising / flat / a zero endpoint
        LINEAR:  0.5 * (c1 + c2) * (t2 - t1)
    else:                                        # both > 0 and strictly falling
        LOG:     (t2 - t1) * (c1 - c2) / ln(c1 / c2)

The log-down form is the exact integral of the log-linear interpolant
c(t) = c1 * exp(-k (t - t1)) with k = ln(c1/c2)/(t2-t1); it is therefore
EXACT on a true mono-exponential decay.  Equal consecutive values route to the
LINEAR branch (c2 >= c1), which also avoids ln(1)=0 in the denominator.

t=0 handling: if the first sample time > 0 and no concentration at t=0 is
supplied, integration STARTS at the first available point — a 0-hour point is
never fabricated.  If a measurable 0-hour value exists it is used; a BLQ 0-hour
sample becomes the (0, 0) start point (leading-BLQ rule).

lambda_z — terminal elimination rate constant (WinNonlin ARS best-fit)
----------------------------------------------------------------------
Candidate terminal points are the measurable samples with time STRICTLY greater
than Tmax (the Cmax point itself is always excluded) and concentration > 0.
For every window made of the LAST k of those points (k = 3, 4, ... up to all of
them):

  * regress ln(conc) on time by ordinary least squares -> slope m,
    lambda_z = -m, intercept b, R^2 of the ln-linear fit, and
    adjusted R^2 = 1 - (1 - R^2) * (n - 1) / (n - 2).

The chosen window is the one with the HIGHEST adjusted R^2, with WinNonlin's
"Adjusted R-Squared" (ARS) tie rule: among all windows whose adjusted R^2 is
within 0.0001 of the maximum, the one with the MOST points is selected. A
window qualifies only if it has >= 3 points and lambda_z > 0. If no window
qualifies, lambda_z / thalf / auc0inf are None and a "lambda_z not estimable"
flag is added.

    thalf   = ln(2) / lambda_z

AUC0-inf and %AUCextrap
-----------------------
    AUC0-inf   = AUC0-t + Clast / lambda_z        (Clast = OBSERVED last conc)
    %AUCextrap = 100 * (AUC0-inf - AUC0-t) / AUC0-inf

A flag is added when %AUCextrap exceeds 20% (an unreliable extrapolation).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Optional

# Tie tolerance for WinNonlin's Adjusted-R-Squared terminal-slope selection.
_ARS_TIE_TOL = 1e-4

AUC0T_METHOD = "linear-up/log-down"


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
@dataclass
class NCAResult:
    """Non-compartmental analysis result for a single concentration-time profile.

    ``None`` fields mean "not determinable from these data" (e.g. no measurable
    concentrations, or lambda_z not estimable).
    """

    cmax: Optional[float]
    tmax: Optional[float]
    auc0t: Optional[float]
    auc0inf: Optional[float]
    lambda_z: Optional[float]
    thalf: Optional[float]
    pct_extrap: Optional[float]
    clast: Optional[float]
    tlast: Optional[float]
    auc0t_method: str = AUC0T_METHOD
    lambda_z_n_points: int = 0
    lambda_z_r2: Optional[float] = None
    lambda_z_r2adj: Optional[float] = None
    lambda_z_intercept: Optional[float] = None
    flags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """Plain-dict view (JSON-friendly), suitable for API responses."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------
def _is_measurable(conc, lloq: float) -> bool:
    """True iff ``conc`` is a quantifiable, positive concentration (>= lloq, > 0)."""
    return conc is not None and conc >= lloq and conc > 0.0


def _trapezoid(t1: float, c1: float, t2: float, c2: float) -> float:
    """Area of one interval under the linear-up / log-down rule."""
    dt = t2 - t1
    if c2 >= c1 or c1 <= 0.0 or c2 <= 0.0:
        # rising, flat, or a non-positive endpoint -> linear trapezoid
        return 0.5 * (c1 + c2) * dt
    # both strictly positive and strictly decreasing -> log trapezoid
    return dt * (c1 - c2) / math.log(c1 / c2)


def _ols_ln(times: list, concs: list):
    """Ordinary least squares of ln(conc) on time.

    Returns (slope, intercept, r2, r2adj).  ``r2`` is clamped to <= 1.0 to
    absorb floating-point overshoot on near-perfect fits.  Assumes len >= 3 and
    at least two distinct times (guaranteed by the caller).
    """
    n = len(times)
    ys = [math.log(c) for c in concs]
    xbar = sum(times) / n
    ybar = sum(ys) / n
    sxx = sum((x - xbar) ** 2 for x in times)
    sxy = sum((x - xbar) * (y - ybar) for x, y in zip(times, ys))
    syy = sum((y - ybar) ** 2 for y in ys)

    slope = sxy / sxx
    intercept = ybar - slope * xbar

    if syy <= 0.0:
        # Degenerate: perfectly flat ln(conc). slope ~ 0 so this window is
        # rejected downstream (lambda_z <= 0); report r2 = 0 conservatively.
        r2 = 0.0
    else:
        r2 = (sxy * sxy) / (sxx * syy)
        if r2 > 1.0:
            r2 = 1.0
    r2adj = 1.0 - (1.0 - r2) * (n - 1) / (n - 2)
    return slope, intercept, r2, r2adj


def _estimate_lambda_z(terminal: list):
    """WinNonlin ARS best-fit terminal slope.

    ``terminal`` is a time-sorted list of (t, c) with c > 0, all strictly after
    Tmax.  Returns a dict describing the selected window, or None if lambda_z is
    not estimable.
    """
    n_total = len(terminal)
    if n_total < 3:
        return None

    candidates = []
    for k in range(3, n_total + 1):
        window = terminal[n_total - k:]  # the LAST k points
        times = [p[0] for p in window]
        concs = [p[1] for p in window]
        slope, intercept, r2, r2adj = _ols_ln(times, concs)
        lam = -slope
        if lam <= 0.0:
            continue  # require lambda_z > 0 (genuinely declining terminal phase)
        candidates.append(
            {
                "n": k,
                "lambda_z": lam,
                "r2": r2,
                "r2adj": r2adj,
                "intercept": intercept,
            }
        )

    if not candidates:
        return None

    best_adj = max(c["r2adj"] for c in candidates)
    # ARS tie rule: among windows within 0.0001 of the best adjusted R^2,
    # prefer the one with the most points.
    within = [c for c in candidates if c["r2adj"] >= best_adj - _ARS_TIE_TOL]
    return max(within, key=lambda c: c["n"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def compute_nca(time_conc, *, lloq: float, blq_rule: str = "std") -> NCAResult:
    """Compute NCA parameters for one concentration-time profile.

    Parameters
    ----------
    time_conc : list[tuple[float, float | None]]
        (time, concentration) samples.  ``None`` concentration means BLQ.
        Need not be sorted — sorted defensively by time here.
    lloq : float
        Lower limit of quantification.  A sample is measurable iff its value is
        not None, is >= lloq, and is > 0.
    blq_rule : str
        Only ``"std"`` (the standard regulatory convention) is supported.

    Returns
    -------
    NCAResult
    """
    if blq_rule != "std":
        raise ValueError(f"Unsupported blq_rule: {blq_rule!r} (only 'std' is supported)")

    pts = sorted(time_conc, key=lambda tc: tc[0])
    flags: list = []

    measurable = [_is_measurable(c, lloq) for (_, c) in pts]

    if not any(measurable):
        flags.append("no measurable concentrations")
        return NCAResult(
            cmax=None, tmax=None, auc0t=None, auc0inf=None,
            lambda_z=None, thalf=None, pct_extrap=None,
            clast=None, tlast=None, flags=flags,
        )

    first_meas_idx = measurable.index(True)
    last_meas_idx = max(i for i, m in enumerate(measurable) if m)

    # --- Build the retained-point list for AUC integration --------------
    # leading BLQ -> (t, 0) kept; measurable kept; embedded/trailing BLQ dropped.
    retained: list = []
    embedded_dropped = 0
    trailing_dropped = 0
    for i, (t, c) in enumerate(pts):
        if measurable[i]:
            retained.append((t, c))
        elif i < first_meas_idx:
            retained.append((t, 0.0))          # leading BLQ set to zero
        elif i < last_meas_idx:
            embedded_dropped += 1              # embedded BLQ -> missing
        else:
            trailing_dropped += 1              # trailing BLQ -> missing
    if embedded_dropped:
        flags.append(f"embedded BLQ dropped as missing (n={embedded_dropped})")
    if trailing_dropped:
        flags.append(f"trailing BLQ dropped as missing (n={trailing_dropped})")

    # --- Cmax / Tmax (first time the maximum measurable value occurs) ---
    cmax: Optional[float] = None
    tmax: Optional[float] = None
    for i, (t, c) in enumerate(pts):
        if measurable[i] and (cmax is None or c > cmax):
            cmax, tmax = c, t

    # --- Clast / Tlast (last measurable, > 0) ---------------------------
    tlast = pts[last_meas_idx][0]
    clast = pts[last_meas_idx][1]

    # --- AUC0-t (linear-up / log-down), first time -> Tlast -------------
    auc0t = 0.0
    for (t1, c1), (t2, c2) in zip(retained, retained[1:]):
        auc0t += _trapezoid(t1, c1, t2, c2)

    # --- lambda_z: terminal points strictly after Tmax ------------------
    terminal = [
        (pts[i][0], pts[i][1])
        for i in range(len(pts))
        if measurable[i] and pts[i][0] > tmax
    ]
    sel = _estimate_lambda_z(terminal)

    if sel is None:
        flags.append("lambda_z not estimable")
        return NCAResult(
            cmax=cmax, tmax=tmax, auc0t=auc0t, auc0inf=None,
            lambda_z=None, thalf=None, pct_extrap=None,
            clast=clast, tlast=tlast, lambda_z_n_points=0, flags=flags,
        )

    lambda_z = sel["lambda_z"]
    thalf = math.log(2.0) / lambda_z
    auc0inf = auc0t + clast / lambda_z
    pct_extrap = 100.0 * (auc0inf - auc0t) / auc0inf if auc0inf > 0.0 else None
    if pct_extrap is not None and pct_extrap > 20.0:
        flags.append(
            f"high terminal extrapolation: %AUCextrap={pct_extrap:.2f}% (>20%)"
        )

    return NCAResult(
        cmax=cmax, tmax=tmax, auc0t=auc0t, auc0inf=auc0inf,
        lambda_z=lambda_z, thalf=thalf, pct_extrap=pct_extrap,
        clast=clast, tlast=tlast,
        lambda_z_n_points=sel["n"], lambda_z_r2=sel["r2"],
        lambda_z_r2adj=sel["r2adj"], lambda_z_intercept=sel["intercept"],
        flags=flags,
    )


def compute_profiles(profiles, *, lloq: float, blq_rule: str = "std") -> dict:
    """Run :func:`compute_nca` over many profiles.

    Parameters
    ----------
    profiles : dict[hashable, list[tuple[float, float | None]]]
        Typically keyed by ``(subject, period)``.
    lloq, blq_rule : see :func:`compute_nca`.

    Returns
    -------
    dict[hashable, NCAResult]
        Same keys as ``profiles``.
    """
    return {
        key: compute_nca(series, lloq=lloq, blq_rule=blq_rule)
        for key, series in profiles.items()
    }
