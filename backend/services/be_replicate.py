"""FDA Reference-Scaled Average Bioequivalence (RSABE) — analysis of REPLICATE
designs for highly variable drug products (HVDs).

Where :mod:`services.be_analysis` analyses a 2x2 crossover / parallel study with
conventional average bioequivalence (ABE), this module implements the harder,
higher-risk **reference-scaled** decision the FDA requires when the reference
product is *highly variable* (within-subject CV of the reference, CVwR, >= 30%).
The acceptance limits are then *scaled* to the observed reference variability
instead of the fixed 80.00-125.00% goalposts.

Method (FDA RSABE — Howe / Hyslop-Hsuan-Holder linearized criterion)
--------------------------------------------------------------------
Work on ln(value). In a replicate design each subject receives the Reference at
least twice (and Test once or twice).

  * Per subject i: mean_T_i = mean of ln(T) replicates, mean_R_i = mean of
    ln(R) replicates, and the within-subject difference D_i = mean_T_i - mean_R_i.
    Over the ``n`` subjects that have both, the point estimate of ln(GMR) is
    ``Dbar = mean(D_i)`` (GMR = exp(Dbar)) and ``s2D`` is the sample variance of
    the D_i (ddof = 1).
  * Within-subject variance of the Reference, pooled over subjects::

        s2wR = sum_i sum_j (ln R_ij - mean_R_i)^2 / dfwR,
        dfwR = sum_i (nR_i - 1)

    and ``CVwR = sqrt(exp(s2wR) - 1)``. (This is the subject-centred pooled
    estimator specified for this module. The FDA / EMA ANOVA estimator
    additionally removes the period effect; on the balanced reference datasets
    the two agree to ~1% relative — see ``provenance['crosscheck_replicateBE']``.)
  * Regulatory constants: sigma_w0 = 0.25, theta = (ln(1.25)/sigma_w0)^2 ~= 0.7967.

Scaled vs unscaled switch (implemented rule + citation)
-------------------------------------------------------
FDA's RSABE applies **only** once the reference product is highly variable. This
module switches on **CVwR >= 30%** (equivalently s2wR >= ln(1.09) ~= 0.08618):

  * ``CVwR >= 30%``  -> reference-scaled ABE (the linearized criterion below);
  * ``CVwR < 30%``   -> conventional unscaled ABE (the 90% CI of the treatment
    ratio from the fixed-effects log-ANOVA of the replicate data must fall inside
    80.00-125.00%).

Citation: FDA, "Draft Guidance on Progesterone" (2011) and "Bioequivalence
Studies with Pharmacokinetic Endpoints for Drugs Submitted Under an ANDA" (Dec
2013); Howe WG (1974) JASA 69:789; Hyslop T, Hsuan F, Holder DJ (2000)
"A small-sample confidence interval approach to assess individual bioequivalence"
Stat Med 19:2885. (Some FDA texts state the switch as s2wR >= 0.0894, i.e.
CVwR ~= 30.6%, using the reference-scaling constant swR0 = 0.25; this module uses
the CVwR >= 30% form, the more conservative and the one most commonly cited.)

RSABE linearized criterion + 95% upper bound (method of moments)
----------------------------------------------------------------
    point estimate  eta_hat = Dbar^2 - theta * s2wR
    Em = Dbar^2 ;   Hm = (|Dbar| + t_(0.95, n-1) * sqrt(s2D/n))^2 - Dbar^2
    Es = theta*s2wR ; Hs = theta*s2wR * (dfwR / chisq_(0.05, dfwR) - 1)
    Cbeta (95% upper bound) = eta_hat + sqrt(Hm^2 + Hs^2)

**PASS (scaled)** iff ``Cbeta <= 0`` AND the point estimate GMR is within
80.00-125.00% (the FDA point-estimate constraint).

Purity / validation
-------------------
Pure Python (stdlib ``math`` only) for the engine, reusing the already-validated
``_t_quantile`` from :mod:`services.biostatistics`. A local chi-square quantile
(inverse regularized lower incomplete gamma) is implemented here and validated in
the test-suite against tabulated values and against R's ``qchisq``.

Every analysis carries a best-effort R cross-check in ``provenance`` that
reproduces the *identical* moment criterion in base R (using R's ``qt`` /
``qchisq``), so the pure-Python ``Cbeta`` is checked against an independent engine
on every call. NOTE on scope: the community package ``replicateBE`` (v1.1.3,
installed in the project R library) implements the **EMA ABEL** method, **not**
the FDA RSABE linearized criterion (its ``method.A(regulator="FDA")`` path is
non-functional in that version). replicateBE is therefore used to cross-check the
shared quantities (point estimate / CVwR), while the FDA ``Cbeta`` itself is
validated by exact reproduction in base R. See the module test-suite and the
delivery report for the precise validation status. NTID (narrow-therapeutic-index)
drugs need the additional FDA variance-ratio bound and sigma_w0 = 0.10 scaling on
fully-replicated data; that method is intentionally **out of scope** here (see
:func:`analyze_replicate`).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Sequence

# Reuse the already-validated (no-SciPy) central-t quantile and the R bridge.
from services.biostatistics import _t_quantile
from services.powertost_r import _run_r, _find_rscript, _RLIB  # noqa: F401 (_RLIB kept for provenance)

# ---------------------------------------------------------------------------
# Regulatory constants
# ---------------------------------------------------------------------------
SIGMA_W0 = 0.25                                   # FDA reference-scaling constant
THETA_RSABE = (math.log(1.25) / SIGMA_W0) ** 2    # ~= 0.79688 goalpost coefficient
CVWR_SWITCH = 0.30                                # scale only when CVwR >= 30%
S2WR_SWITCH = math.log(1.0 + CVWR_SWITCH ** 2)    # equivalent s2wR threshold ~=0.08618
_ABE_LIMITS = (80.00, 125.00)                     # point-estimate / unscaled goalposts
_SINGULAR_PIVOT_TOL = 1e-9


# ---------------------------------------------------------------------------
# Special functions — regularized lower incomplete gamma + chi-square quantile
# ---------------------------------------------------------------------------
def _gammp_series(a: float, x: float) -> float:
    """Lower regularized incomplete gamma P(a, x) via its series (x < a+1)."""
    ap = a
    term = 1.0 / a
    total = term
    for _ in range(1000):
        ap += 1.0
        term *= x / ap
        total += term
        if abs(term) < abs(total) * 1e-16:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gammq_cf(a: float, x: float) -> float:
    """Upper regularized incomplete gamma Q(a, x) via the Lentz continued
    fraction (x >= a+1). P(a,x) = 1 - Q(a,x)."""
    FPMIN = 1.0e-300
    b = x + 1.0 - a
    c = 1.0 / FPMIN
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < FPMIN:
            d = FPMIN
        c = b + an / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def _gammp(a: float, x: float) -> float:
    """Regularized lower incomplete gamma function P(a, x)."""
    if x < 0.0 or a <= 0.0:
        raise ValueError("_gammp requires a > 0 and x >= 0.")
    if x == 0.0:
        return 0.0
    if x < a + 1.0:
        return _gammp_series(a, x)
    return 1.0 - _gammq_cf(a, x)


def _chisq_cdf(x: float, df: float) -> float:
    """CDF of the chi-square distribution: P(X^2 <= x | df) = P(df/2, x/2)."""
    if x <= 0.0:
        return 0.0
    return _gammp(df / 2.0, x / 2.0)


def _chisq_quantile(p: float, df: float) -> float:
    """Inverse chi-square CDF: the value x with P(X^2 <= x | df) = p.

    Inverts the regularized lower incomplete gamma by monotone bisection (the
    chi-square CDF is strictly increasing in x). Accurate to full double
    precision; validated against tabulated values and R ``qchisq`` in the tests.
    """
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1).")
    if df <= 0:
        raise ValueError("df must be positive.")
    a = df / 2.0
    lo = 0.0
    hi = max(df + 10.0 * math.sqrt(2.0 * df) + 20.0, 1.0)
    while _gammp(a, hi / 2.0) < p:          # widen until it brackets p
        hi *= 2.0
        if hi > 1e12:
            break
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _gammp(a, mid / 2.0) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
@dataclass
class ReplicateBEResult:
    """One metric's reference-scaled (or unscaled-fallback) BE result.

    Shape is compatible in spirit with :class:`services.be_analysis.BEResult`
    plus the scaled-specific fields required for an RSABE submission.
    """
    metric: str
    n: int                              # subjects with both T and R (contribute D_i)
    point_estimate_pct: float           # 100 * GMR, GMR = exp(Dbar)
    cv_wr_pct: float                    # within-subject CV of Reference (%)
    cv_intra_pct: float                 # alias of cv_wr_pct (BEResult compatibility)
    s2wr: float                         # within-subject variance of Reference (ln)
    scaled: bool                        # was reference-scaling applied?
    criterion_upper_bound: Optional[float]   # Cbeta (None when unscaled)
    within_be_limits: bool              # the PASS/FAIL decision
    ci_lower_pct: Optional[float]       # conventional 90% CI (fixed-effects ANOVA)
    ci_upper_pct: Optional[float]
    lower_limit_pct: float
    upper_limit_pct: float
    df: int                             # df of the point estimate (n - 1)
    se_ln: float                        # SE of Dbar on the ln scale = sqrt(s2D/n)
    method: str
    model: str
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Validation / record handling
# ---------------------------------------------------------------------------
def _validate_records(records: Sequence[dict]) -> None:
    if not records:
        raise ValueError("No records supplied.")
    for r in records:
        if r.get("treatment") not in ("T", "R"):
            raise ValueError(
                f"treatment must be 'T' or 'R', got {r.get('treatment')!r} "
                f"for subject {r.get('subject')!r}."
            )
        v = r.get("value")
        if v is None or not isinstance(v, (int, float)) or v <= 0 or not math.isfinite(v):
            raise ValueError(
                f"value must be a positive finite number (original scale), got "
                f"{v!r} for subject {r.get('subject')!r}."
            )


def _subject_stats(records: Sequence[dict]) -> dict:
    """Per-subject ln-scale summaries and the pooled reference variance.

    Returns Dbar, s2D, n, s2wR, dfwR, CVwR plus the raw per-subject arrays for
    provenance. Subjects are visited in first-appearance order for determinism.
    """
    order: list[str] = []
    tvals: dict[str, list[float]] = {}
    rvals: dict[str, list[float]] = {}
    for r in records:
        s = str(r["subject"])
        if s not in tvals:
            tvals[s] = []
            rvals[s] = []
            order.append(s)
        (tvals if r["treatment"] == "T" else rvals)[s].append(math.log(r["value"]))

    # Within-subject differences over subjects that have BOTH treatments.
    diffs: list[float] = []
    diff_subjects: list[str] = []
    for s in order:
        if tvals[s] and rvals[s]:
            mt = sum(tvals[s]) / len(tvals[s])
            mr = sum(rvals[s]) / len(rvals[s])
            diffs.append(mt - mr)
            diff_subjects.append(s)
    n = len(diffs)
    if n < 2:
        raise ValueError(
            "A replicate RSABE analysis needs at least 2 subjects with both a "
            f"Test and a Reference administration (got {n})."
        )
    dbar = sum(diffs) / n
    s2d = sum((d - dbar) ** 2 for d in diffs) / (n - 1)      # ddof = 1

    # Pooled within-subject variance of Reference over its replicates.
    ss_wr = 0.0
    df_wr = 0
    n_r_reps = 0
    for s in order:
        rs = rvals[s]
        if len(rs) >= 1:
            n_r_reps += len(rs)
            if len(rs) >= 2:
                mr = sum(rs) / len(rs)
                ss_wr += sum((x - mr) ** 2 for x in rs)
                df_wr += len(rs) - 1
    if df_wr < 1:
        raise ValueError(
            "Cannot estimate the within-subject variance of Reference: no "
            "subject has 2+ Reference replicates (not a replicate design)."
        )
    s2wr = ss_wr / df_wr
    cvwr = math.sqrt(math.exp(s2wr) - 1.0)

    return {
        "n": n,
        "dbar": dbar,
        "s2d": s2d,
        "s2wr": s2wr,
        "df_wr": df_wr,
        "cvwr": cvwr,
        "diffs": diffs,
        "diff_subjects": diff_subjects,
        "n_r_reps": n_r_reps,
    }


# ---------------------------------------------------------------------------
# Conventional (unscaled) ABE 90% CI — fixed-effects log-ANOVA, == R lm()
# ---------------------------------------------------------------------------
def _mat_inv(A: Sequence[Sequence[float]]) -> list[list[float]]:
    """Invert a square matrix by Gauss-Jordan elimination with partial pivoting."""
    n = len(A)
    M = [list(A[i]) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot_row][col]) < _SINGULAR_PIVOT_TOL:
            raise ValueError("OLS design matrix is singular (rank-deficient).")
        if pivot_row != col:
            M[col], M[pivot_row] = M[pivot_row], M[col]
        inv_pivot = 1.0 / M[col][col]
        rc = M[col]
        for j in range(2 * n):
            rc[j] *= inv_pivot
        for r in range(n):
            if r != col:
                f = M[r][col]
                if f != 0.0:
                    rr = M[r]
                    for j in range(2 * n):
                        rr[j] -= f * rc[j]
    return [row[n:] for row in M]


def _conventional_abe_ci(records: Sequence[dict], alpha: float = 0.05) -> Optional[dict]:
    """90% CI of the T-R ratio from ``ln(value) ~ subject + period + treatment``.

    Fixed-effects (subjects treated as fixed) OLS with reference/dummy coding —
    identical to R ``lm(log(value) ~ factor(subject)+factor(period)+factor(treatment))``.
    Returns ``{pe, ci_lo, ci_hi, df, se_ln}`` (ratios) or ``None`` if the model is
    not estimable (e.g. no period column). Never raises — the RSABE decision must
    not depend on this supportive interval.
    """
    try:
        if any(r.get("period") is None for r in records):
            return None
        subjects = sorted({str(r["subject"]) for r in records})
        periods = sorted({r["period"] for r in records})
        if len(subjects) < 2 or len(periods) < 2:
            return None
        other_subjects = subjects[1:]
        nonref_periods = periods[1:]
        t_idx = 1
        per_col = {p: 2 + k for k, p in enumerate(nonref_periods)}
        sub_col = {s: 2 + len(nonref_periods) + k for k, s in enumerate(other_subjects)}
        p_cols = 2 + len(nonref_periods) + len(other_subjects)

        X: list[list[float]] = []
        y: list[float] = []
        for r in records:
            row = [0.0] * p_cols
            row[0] = 1.0
            if r["treatment"] == "T":
                row[t_idx] = 1.0
            if r["period"] in per_col:
                row[per_col[r["period"]]] = 1.0
            s = str(r["subject"])
            if s in sub_col:
                row[sub_col[s]] = 1.0
            X.append(row)
            y.append(math.log(r["value"]))

        n_obs = len(X)
        p = p_cols
        df = n_obs - p
        if df <= 0:
            return None
        XtX = [[0.0] * p for _ in range(p)]
        XtY = [0.0] * p
        for i in range(n_obs):
            xi = X[i]
            yi = y[i]
            for a in range(p):
                if xi[a]:
                    XtY[a] += xi[a] * yi
                    row_a = XtX[a]
                    for b in range(p):
                        if xi[b]:
                            row_a[b] += xi[a] * xi[b]
        XtX_inv = _mat_inv(XtX)
        beta = [sum(XtX_inv[i][j] * XtY[j] for j in range(p)) for i in range(p)]
        rss = 0.0
        for i in range(n_obs):
            xi = X[i]
            fitted = sum(xi[a] * beta[a] for a in range(p) if xi[a])
            rss += (y[i] - fitted) ** 2
        mse = rss / df
        beta_t = beta[t_idx]
        se_ln = math.sqrt(mse * XtX_inv[t_idx][t_idx])
        tcrit = _t_quantile(1.0 - alpha, df)
        return {
            "pe": math.exp(beta_t),
            "ci_lo": math.exp(beta_t - tcrit * se_ln),
            "ci_hi": math.exp(beta_t + tcrit * se_ln),
            "df": df,
            "se_ln": se_ln,
        }
    except Exception:  # noqa: BLE001 — supportive only, must never break RSABE
        return None


# ---------------------------------------------------------------------------
# Core RSABE criterion
# ---------------------------------------------------------------------------
def _rsabe_criterion(stats: dict, alpha: float = 0.05) -> dict:
    """Howe / Hyslop-Hsuan-Holder linearized 95% upper bound for RSABE."""
    n = stats["n"]
    dbar = stats["dbar"]
    s2d = stats["s2d"]
    s2wr = stats["s2wr"]
    df_wr = stats["df_wr"]

    eta_hat = dbar * dbar - THETA_RSABE * s2wr
    t_crit = _t_quantile(1.0 - alpha, n - 1)             # qt(0.95, n-1)
    se_dbar = math.sqrt(s2d / n)
    hm = (abs(dbar) + t_crit * se_dbar) ** 2 - dbar * dbar
    chi_lo = _chisq_quantile(alpha, df_wr)               # qchisq(0.05, dfwR)
    hs = THETA_RSABE * s2wr * (df_wr / chi_lo - 1.0)
    cbeta = eta_hat + math.sqrt(hm * hm + hs * hs)
    return {
        "eta_hat": eta_hat,
        "t_crit": t_crit,
        "se_dbar": se_dbar,
        "hm": hm,
        "chisq_lo_005": chi_lo,
        "hs": hs,
        "cbeta": cbeta,
    }


# ---------------------------------------------------------------------------
# R cross-check (best-effort) — independent reproduction of the criterion
# ---------------------------------------------------------------------------
def _r_crosscheck(records: Sequence[dict]) -> dict:
    """Reproduce the moment criterion AND the conventional ABE CI in base R.

    Uses R's own ``qt`` / ``qchisq`` and, when available, ``replicateBE`` for an
    independent point-estimate / CVwR check. Returns a provenance dict; never
    raises.
    """
    rscript = _find_rscript()
    if not rscript:
        return {"r_available": False, "error": "Rscript not found on this host."}

    subs = ",".join('"%s"' % str(r["subject"]) for r in records)
    pers = ",".join(("NA" if r.get("period") is None else str(int(r["period"]))) for r in records)
    trts = ",".join('"%s"' % str(r["treatment"]) for r in records)
    vals = ",".join(repr(float(r["value"])) for r in records)

    body = (
        f'd <- data.frame(subject=c({subs}), period=c({pers}), '
        f'treatment=c({trts}), value=c({vals}), stringsAsFactors=FALSE)\n'
        'd$lg <- log(d$value)\n'
        'Td <- d[d$treatment=="T",]; Rd <- d[d$treatment=="R",]\n'
        'mT <- tapply(Td$lg, Td$subject, mean); mR <- tapply(Rd$lg, Rd$subject, mean)\n'
        'sj <- intersect(names(mT), names(mR)); D <- mT[sj]-mR[sj]\n'
        'n <- length(D); Dbar <- mean(D); s2D <- var(D)\n'
        'ss <- 0; dfwr <- 0\n'
        'for (s in unique(Rd$subject)) { v <- Rd$lg[Rd$subject==s]; '
        'ss <- ss + sum((v-mean(v))^2); dfwr <- dfwr + (length(v)-1) }\n'
        's2wr <- ss/dfwr\n'
        'th <- (log(1.25)/0.25)^2\n'
        'eta <- Dbar^2 - th*s2wr\n'
        'Hm <- (abs(Dbar) + qt(0.95, n-1)*sqrt(s2D/n))^2 - Dbar^2\n'
        'Hs <- th*s2wr*(dfwr/qchisq(0.05, dfwr) - 1)\n'
        'Cb <- eta + sqrt(Hm^2 + Hs^2)\n'
        'CVwR <- sqrt(exp(s2wr)-1)\n'
        # conventional fixed-effects ANOVA CI (== replicateBE Method A CL)
        'ci_lo <- NA; ci_hi <- NA; pe_anova <- NA\n'
        'try({ d2 <- d; d2$treatment <- relevel(factor(d2$treatment), ref="R"); '
        'm <- lm(lg ~ factor(subject) + factor(period) + treatment, data=d2); '
        'est <- coef(m)[["treatmentT"]]; ci <- confint(m, "treatmentT", level=0.90); '
        'pe_anova <- exp(est); ci_lo <- exp(ci[1]); ci_hi <- exp(ci[2]) }, silent=TRUE)\n'
        'cat(sprintf("REPBE|Dbar=%.10f|GMR=%.10f|s2D=%.10f|s2wr=%.10f|dfwr=%d|'
        'CVwR=%.10f|eta=%.10f|Hm=%.10f|Hs=%.10f|Cbeta=%.10f|cilo=%.10f|cihi=%.10f|peanova=%.10f",\n'
        '  Dbar, exp(Dbar), s2D, s2wr, as.integer(dfwr), CVwR, eta, Hm, Hs, Cb, '
        'ci_lo, ci_hi, pe_anova))\n'
    )
    try:
        out, err, rc = _run_r(rscript, body, timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        return {"r_available": False, "error": f"R invocation failed: {exc}"}

    m = re.search(
        r"REPBE\|Dbar=([-0-9.eE+]+)\|GMR=([-0-9.eE+]+)\|s2D=([-0-9.eE+]+)\|"
        r"s2wr=([-0-9.eE+]+)\|dfwr=(-?\d+)\|CVwR=([-0-9.eE+]+)\|eta=([-0-9.eE+]+)\|"
        r"Hm=([-0-9.eE+]+)\|Hs=([-0-9.eE+]+)\|Cbeta=([-0-9.eE+]+)\|"
        r"cilo=([-0-9.eENA+]+)\|cihi=([-0-9.eENA+]+)\|peanova=([-0-9.eENA+]+)",
        out,
    )
    if not m:
        return {"r_available": False,
                "error": (out.strip()[:200] or err.strip()[:200] or "no R output")}

    def _f(s: str) -> Optional[float]:
        try:
            return float(s)
        except ValueError:
            return None

    return {
        "r_available": True,
        "engine": "base R (qt/qchisq) + lm()",
        "rscript": rscript,
        "dbar": _f(m.group(1)),
        "gmr": _f(m.group(2)),
        "s2d": _f(m.group(3)),
        "s2wr": _f(m.group(4)),
        "df_wr": int(m.group(5)),
        "cvwr": _f(m.group(6)),
        "eta_hat": _f(m.group(7)),
        "hm": _f(m.group(8)),
        "hs": _f(m.group(9)),
        "cbeta": _f(m.group(10)),
        "ci_lower_pct": (None if _f(m.group(11)) is None else 100.0 * _f(m.group(11))),
        "ci_upper_pct": (None if _f(m.group(12)) is None else 100.0 * _f(m.group(12))),
        "anova_pe_pct": (None if _f(m.group(13)) is None else 100.0 * _f(m.group(13))),
    }


def _replicatebe_crosscheck(records: Sequence[dict]) -> dict:
    """Cross-check the point estimate & CVwR against the ``replicateBE`` reference
    package's **Method A**.

    replicateBE's ``method.A`` reads only its own bundled datasets through the
    ``data=`` argument (it relies on their internal attributes and rejects a
    reconstructed frame with "argument is of length zero"), so it cannot be
    called on arbitrary input. Instead this reproduces replicateBE's *documented*
    Method A models verbatim::

        CVwR:  lm(log(PK) ~ sequence + sequence:subject + period, R-only)  -> MSE
        PE:    lm(log(PK) ~ sequence + sequence:subject + period + treatment)

    which is verified to give numbers **identical** to the package on its own
    datasets (rds01/02/04: PE and CVwR match to the printed precision). The
    package's presence and version are reported so the check is anchored to the
    installed reference implementation. NOTE: replicateBE 1.1.3 implements EMA
    ABEL, NOT the FDA RSABE criterion, so this validates the *shared* quantities
    (point estimate, ANOVA CVwR) only. Never raises."""
    rscript = _find_rscript()
    if not rscript:
        return {"available": False, "error": "Rscript not found."}
    if any(r.get("period") is None for r in records):
        return {"available": False, "error": "period required for the ANOVA models."}
    if not all(r.get("sequence") for r in records):
        return {"available": False, "error": "sequence required for Method A models."}
    subs = ",".join('"%s"' % str(r["subject"]) for r in records)
    pers = ",".join(str(int(r["period"])) for r in records)
    seqs = ",".join('"%s"' % str(r["sequence"]) for r in records)
    trts = ",".join('"%s"' % str(r["treatment"]) for r in records)
    vals = ",".join(repr(float(r["value"])) for r in records)
    body = (
        'has <- requireNamespace("replicateBE", quietly=TRUE)\n'
        'ver <- if (has) as.character(packageVersion("replicateBE")) else "absent"\n'
        f'd <- data.frame(subject=factor(c({subs})), period=factor(c({pers})), '
        f'sequence=factor(c({seqs})), treatment=c({trts}), PK=c({vals}))\n'
        'd$lg <- log(d$PK)\n'
        'Rd <- d[d$treatment=="R",]\n'
        'pe <- NA; cvwr <- NA\n'
        'try({ mR <- lm(lg ~ sequence + sequence:subject + period, data=Rd); '
        's2 <- sum(residuals(mR)^2)/mR$df.residual; cvwr <- sqrt(exp(s2)-1) }, silent=TRUE)\n'
        'try({ d$trt <- relevel(factor(d$treatment), ref="R"); '
        'mF <- lm(lg ~ sequence + sequence:subject + period + trt, data=d); '
        'pe <- exp(coef(mF)[["trtT"]]) }, silent=TRUE)\n'
        'cat(sprintf("REPBEA|PE=%.10f|CVwR=%.10f|ver=%s", pe, cvwr, ver))\n'
    )
    try:
        out, err, rc = _run_r(rscript, body, timeout=90.0)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": f"R invocation failed: {exc}"}
    m = re.search(r"REPBEA\|PE=([-0-9.eENA+]+)\|CVwR=([-0-9.eENA+]+)\|ver=(\S+)", out)
    if not m:
        return {"available": False,
                "error": (out.strip()[:200] or err.strip()[:200] or "no output")}
    try:
        pe = float(m.group(1))
        cvwr = float(m.group(2))
    except ValueError:
        return {"available": False, "error": f"non-numeric R output: {out.strip()[:200]}"}
    return {
        "available": True,
        "package_installed": (m.group(3) != "absent"),
        "package_version": m.group(3),
        "method": ("replicateBE Method A models reproduced in base R (verified "
                   "identical to the package on its bundled datasets)"),
        "note": ("replicateBE implements EMA ABEL, NOT the FDA RSABE criterion; "
                 "used here only to cross-check the point estimate and the ANOVA "
                 "CVwR. Its CVwR removes the period effect, whereas this module's "
                 "s2wR is the subject-centred pooled estimator, so a small "
                 "difference in CVwR is expected and not an error."),
        "point_estimate_pct": 100.0 * pe,
        "cvwr_pct": 100.0 * cvwr,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def analyze_replicate(
    records: Sequence[dict], *, metric_name: str = "", is_nti: bool = False
) -> dict:
    """Analyse a replicate-design BE study by the FDA RSABE method.

    ``records``: list of dicts, each
    ``{"subject": str, "sequence": str, "period": int, "treatment": "T"|"R",
    "value": float}`` where ``value`` is the PK metric on the ORIGINAL scale.

    Returns a dict (see :class:`ReplicateBEResult`) carrying the RSABE decision:
    ``point_estimate_pct``, ``cv_wr_pct``, ``s2wr``, ``scaled`` (whether
    reference-scaling was applied), ``criterion_upper_bound`` (Cbeta),
    ``within_be_limits`` (PASS/FAIL) and full ``provenance`` including a
    best-effort independent R reproduction of the criterion.

    * ``CVwR >= 30%`` -> reference-scaled ABE (Howe / Hyslop-Hsuan-Holder
      criterion; PASS iff Cbeta <= 0 AND GMR in 80.00-125.00%).
    * ``CVwR < 30%``  -> conventional unscaled ABE (90% CI of the fixed-effects
      ANOVA within 80.00-125.00%); ``ci_lower_pct`` / ``ci_upper_pct`` populated.

    ``is_nti=True`` is intentionally **not supported**: the FDA NTID method needs
    the additional within-subject variance-ratio upper bound (sigma_WT/sigma_WR)
    and sigma_w0 = 0.10 scaling on fully-replicated data, for which no authoritative
    reference is available to validate against in this environment. Emitting an
    unvalidated NTID pass/fail would be worse than refusing, so this raises
    ``NotImplementedError`` (RSABE for highly variable drugs is the validated
    scope of this module).
    """
    if is_nti:
        raise NotImplementedError(
            "FDA NTID (narrow-therapeutic-index) analysis is out of scope for this "
            "module. NTID requires (1) the reference-scaled criterion with "
            "sigma_w0 = 0.10, (2) the conventional 90% CI within 80.00-125.00%, and "
            "(3) an upper bound on the sigma_WT/sigma_WR variance ratio (<= 2.5) from "
            "FULLY replicated (2x2x4) data. That combined method has no authoritative "
            "reference available here to validate against, and an unvalidated NTID "
            "pass/fail is unsafe. This module implements + validates FDA RSABE for "
            "highly variable drugs only."
        )

    _validate_records(records)
    stats = _subject_stats(records)
    n = stats["n"]
    dbar = stats["dbar"]
    s2d = stats["s2d"]
    s2wr = stats["s2wr"]
    df_wr = stats["df_wr"]
    cvwr = stats["cvwr"]
    gmr = math.exp(dbar)
    point_pct = 100.0 * gmr
    se_ln = math.sqrt(s2d / n)

    # Conventional fixed-effects ANOVA CI (supportive; also the decision path
    # when unscaled). Best-effort — never breaks the RSABE decision.
    anova = _conventional_abe_ci(records)
    ci_lo_pct = 100.0 * anova["ci_lo"] if anova else None
    ci_hi_pct = 100.0 * anova["ci_hi"] if anova else None

    pe_in_limits = _ABE_LIMITS[0] <= point_pct <= _ABE_LIMITS[1]
    scaled = cvwr >= CVWR_SWITCH

    if scaled:
        crit = _rsabe_criterion(stats)
        cbeta = crit["cbeta"]
        within = (cbeta <= 0.0) and pe_in_limits
        method = "FDA RSABE (reference-scaled ABE; Howe/Hyslop-Hsuan-Holder linearized criterion)"
        model = ("ln(PK): Dbar=mean subject (T-R) differences; s2wR pooled within-"
                 "subject Reference variance; 95% upper bound Cbeta by method of moments")
        crit_bound: Optional[float] = cbeta
    else:
        crit = None
        cbeta = None
        crit_bound = None
        method = ("Conventional unscaled ABE (CVwR < 30%; 90% CI of the fixed-"
                  "effects log-ANOVA within 80.00-125.00%)")
        model = "ln(PK) ~ subject + period + treatment (subjects fixed)"
        if anova is None:
            raise ValueError(
                "Unscaled ABE fallback needs an estimable fixed-effects ANOVA "
                "(records must carry 'period'); none could be fitted."
            )
        within = (ci_lo_pct >= _ABE_LIMITS[0]) and (ci_hi_pct <= _ABE_LIMITS[1])

    # ---- provenance (incl. best-effort independent R reproduction) ----------
    r_cross = _r_crosscheck(records)
    repbe = _replicatebe_crosscheck(records)

    validation_note = (
        "Cbeta is checked on every call against an independent base-R reproduction "
        "of the identical moment criterion (R qt/qchisq) — see crosscheck_R. The "
        "point estimate and CVwR are additionally cross-checked against the "
        "replicateBE reference package (crosscheck_replicateBE). replicateBE 1.1.3 "
        "implements EMA ABEL, not the FDA RSABE criterion, so no third-party FDA "
        "Cbeta ground truth exists in this environment; the chi-square quantile and "
        "Cbeta arithmetic are validated in tests against tabulated values and R."
    )

    provenance: dict[str, Any] = {
        "engine": "pure-python RSABE (stdlib math), reusing biostatistics._t_quantile",
        "sigma_w0": SIGMA_W0,
        "theta": THETA_RSABE,
        "switching_rule": f"reference-scale when CVwR >= {CVWR_SWITCH:.0%} "
                          f"(s2wR >= {S2WR_SWITCH:.6f})",
        "regulatory_basis": ("FDA Draft Guidance on Progesterone (2011); FDA "
                             "Bioequivalence Studies with PK Endpoints (2013); "
                             "Howe 1974; Hyslop, Hsuan & Holder 2000 (Stat Med)"),
        "n_subjects_both": n,
        "n_reference_replicates": stats["n_r_reps"],
        "dbar_ln": dbar,
        "gmr": gmr,
        "s2D": s2d,
        "s2wR": s2wr,
        "df_wR": df_wr,
        "cv_wr": cvwr,
        "point_estimate_in_80_125": pe_in_limits,
        "conventional_anova_ci": (
            None if anova is None else
            {"pe_pct": 100.0 * anova["pe"], "ci_lower_pct": ci_lo_pct,
             "ci_upper_pct": ci_hi_pct, "df": anova["df"], "se_ln": anova["se_ln"]}
        ),
        "crosscheck_R": r_cross,
        "crosscheck_replicateBE": repbe,
        "validation": validation_note,
    }
    if crit is not None:
        provenance["criterion"] = {
            "eta_hat": crit["eta_hat"],
            "Em_dbar2": dbar * dbar,
            "Hm": crit["hm"],
            "Es_theta_s2wr": THETA_RSABE * s2wr,
            "Hs": crit["hs"],
            "t_0.95_dfD": crit["t_crit"],
            "chisq_0.05_dfWR": crit["chisq_lo_005"],
            "Cbeta_95_upper": crit["cbeta"],
            "pass_scaled": (crit["cbeta"] <= 0.0) and pe_in_limits,
        }
        # Attach the delta vs the independent R Cbeta when available.
        if r_cross.get("r_available") and r_cross.get("cbeta") is not None:
            provenance["criterion"]["Cbeta_R"] = r_cross["cbeta"]
            provenance["criterion"]["Cbeta_abs_delta_vs_R"] = abs(crit["cbeta"] - r_cross["cbeta"])

    result = ReplicateBEResult(
        metric=metric_name,
        n=n,
        point_estimate_pct=point_pct,
        cv_wr_pct=100.0 * cvwr,
        cv_intra_pct=100.0 * cvwr,
        s2wr=s2wr,
        scaled=scaled,
        criterion_upper_bound=crit_bound,
        within_be_limits=within,
        ci_lower_pct=ci_lo_pct,
        ci_upper_pct=ci_hi_pct,
        lower_limit_pct=_ABE_LIMITS[0],
        upper_limit_pct=_ABE_LIMITS[1],
        df=n - 1,
        se_ln=se_ln,
        method=method,
        model=model,
        provenance=provenance,
    )
    return result.to_dict()
