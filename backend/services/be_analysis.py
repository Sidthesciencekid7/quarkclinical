"""Bioequivalence (BE) analysis engine — analyze OBSERVED subject-level PK data.

Where :mod:`services.biostatistics` sizes a *future* BE study, this module
*analyzes the data a completed study produced*: it takes the per-subject,
per-period PK measurements (Cmax, AUC0-t, AUC0-inf, ...) and returns the
pivotal number of an average-bioequivalence (ABE) submission — the **90 %
confidence interval for the geometric mean ratio (GMR) of Test vs Reference**,
together with the point estimate, intra-subject CV and the pass/fail call
against the regulatory equivalence limits.

Method (the regulatory standard, FDA/EMA/ICH M13A)
--------------------------------------------------
ABE is assessed on the **natural-log scale**. For a 2x2 crossover the model is a
fixed-effects ANOVA::

    ln(PK) ~ subject + period + treatment          (subjects treated as fixed)

which is *identical* to R's::

    lm(log(value) ~ factor(subject) + factor(period) + factor(treatment))

The Test-minus-Reference treatment contrast, back-transformed with ``exp``,
gives the GMR; its 90 % CI (two one-sided 5 % tests) is the equivalence
interval. The engine is implemented in **pure Python** (its own OLS via
Gauss-Jordan matrix inversion) so it runs anywhere the backend runs, and every
crossover analysis is **cross-checked against R's** ``lm()`` when R is present,
with the R result stored verbatim in ``provenance["r_crosscheck"]`` for a
reviewer to re-run. The pure-Python engine reproduces R ``lm()`` to < 1e-7 on
the GMR and both CI bounds (see ``tests/test_be_analysis.py``).

Parallel (two independent groups) designs use the **Welch** unequal-variance
two-sample t-test on the log scale with Satterthwaite degrees of freedom.

No numpy / scipy / pandas — only the standard library plus the special functions
already validated in :mod:`services.biostatistics`.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Sequence

# Reuse the already-validated (no-SciPy) t quantile and the R bridge helpers.
from services.biostatistics import _t_quantile
from services.powertost_r import _run_r, _find_rscript

# ---------------------------------------------------------------------------
# Regulatory equivalence limits (percent of Reference)
# ---------------------------------------------------------------------------
# Standard ABE acceptance range and the tightened range for narrow-therapeutic-
# index (NTI) drugs. 111.11 == 100/0.90 (the exact reciprocal of 90.00).
_ABE_LIMITS = (80.00, 125.00)
_NTI_LIMITS = (90.00, 111.11)

# Pivot magnitude below which the OLS design matrix is treated as rank-deficient.
# For 0/1 BE design matrices a genuinely full-rank system has pivots of order 1;
# a structural rank deficiency collapses a pivot to ~1e-14, so this threshold
# cleanly separates the two without false positives on valid (even unbalanced)
# data.
_SINGULAR_PIVOT_TOL = 1e-9


@dataclass
class BEResult:
    """One metric's bioequivalence result (all percentages are % of Reference)."""

    metric: str
    n: int                       # number of subjects contributing
    point_estimate_pct: float    # 100 * GMR (Test/Reference)
    ci_lower_pct: float          # lower 90 % CI bound, %
    ci_upper_pct: float          # upper 90 % CI bound, %
    cv_intra_pct: float          # intra-subject CV % (between-subject for parallel)
    df: int                      # error degrees of freedom
    se_ln: float                 # SE of the T-R contrast on the ln scale
    method: str
    model: str
    lower_limit_pct: float       # equivalence limit, lower
    upper_limit_pct: float       # equivalence limit, upper
    within_be_limits: bool       # CI fully inside [lower_limit, upper_limit]?
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pure-Python linear algebra
# ---------------------------------------------------------------------------


def _mat_inv(A: Sequence[Sequence[float]]) -> list[list[float]]:
    """Invert a square matrix by Gauss-Jordan elimination with partial pivoting.

    Raises ``ValueError`` if the matrix is singular (rank-deficient design).
    """
    n = len(A)
    # Augment [A | I] with a deep copy so the caller's matrix is untouched.
    M = [list(A[i]) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        # Partial pivoting: swap in the row with the largest |value| in this col.
        pivot_row = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot_row][col]) < _SINGULAR_PIVOT_TOL:
            raise ValueError(
                "OLS design matrix is singular (rank-deficient); the treatment "
                "effect is not estimable from these data."
            )
        if pivot_row != col:
            M[col], M[pivot_row] = M[pivot_row], M[col]
        inv_pivot = 1.0 / M[col][col]
        row_c = M[col]
        for j in range(2 * n):
            row_c[j] *= inv_pivot
        for r in range(n):
            if r != col:
                factor = M[r][col]
                if factor != 0.0:
                    row_r = M[r]
                    for j in range(2 * n):
                        row_r[j] -= factor * row_c[j]
    return [row[n:] for row in M]


def _matvec(M: Sequence[Sequence[float]], v: Sequence[float]) -> list[float]:
    return [sum(M[i][j] * v[j] for j in range(len(v))) for i in range(len(M))]


# ---------------------------------------------------------------------------
# Crossover (2x2) — fixed-effects log-ANOVA, identical to R lm()
# ---------------------------------------------------------------------------


def _validate_records(records: Sequence[dict], *, need_period: bool) -> None:
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
        if need_period and r.get("period") is None:
            raise ValueError(f"period is required for subject {r.get('subject')!r}.")


def analyze_crossover_2x2(
    records: Sequence[dict], *, metric_name: str = "", is_nti: bool = False
) -> BEResult:
    """Average bioequivalence for a 2x2 (or higher-order) crossover.

    ``records``: list of dicts, each
    ``{"subject": str, "sequence": str, "period": int, "treatment": "T"|"R",
    "value": float}`` where ``value`` is the PK metric on the ORIGINAL scale.

    Fits ``ln(value) ~ subject + period + treatment`` by ordinary least squares
    with reference (dummy) coding — identical to R's
    ``lm(log(value) ~ factor(subject) + factor(period) + factor(treatment))`` —
    and returns the GMR with its 90 % confidence interval.
    """
    _validate_records(records, need_period=True)

    subjects = sorted({str(r["subject"]) for r in records})
    if len(subjects) < 2:
        raise ValueError("A crossover analysis needs at least 2 subjects.")
    other_subjects = subjects[1:]                     # reference = subjects[0]

    periods = sorted({r["period"] for r in records})
    if len(periods) < 2:
        raise ValueError("A crossover analysis needs at least 2 distinct periods.")
    nonref_periods = periods[1:]                       # reference = periods[0]

    # ---- Design matrix X (full rank, reference-coded) --------------------
    # Column layout is fixed so the treatment column index is deterministic:
    #   0            : intercept
    #   1            : treatment  (1 if T else 0)          <-- t_idx
    #   2 .. 2+P-2   : period dummies (one per non-reference period)
    #   then         : subject dummies (one per non-reference subject)
    # In a 2x2 design nonref_periods has exactly one element, giving the single
    # "period == period2" indicator. Sequence is deliberately absent: it is
    # perfectly absorbed by the subject dummies (each subject lies in exactly
    # one sequence), which keeps the matrix full rank.
    t_idx = 1
    sub_col = {s: 2 + len(nonref_periods) + k for k, s in enumerate(other_subjects)}
    per_col = {p: 2 + k for k, p in enumerate(nonref_periods)}
    p_cols = 2 + len(nonref_periods) + len(other_subjects)

    X: list[list[float]] = []
    y: list[float] = []
    for r in records:
        row = [0.0] * p_cols
        row[0] = 1.0                                             # intercept
        if r["treatment"] == "T":
            row[t_idx] = 1.0                                     # treatment
        if r["period"] in per_col:
            row[per_col[r["period"]]] = 1.0                      # period
        s = str(r["subject"])
        if s in sub_col:
            row[sub_col[s]] = 1.0                                # subject
        X.append(row)
        y.append(math.log(r["value"]))

    n_obs = len(X)
    p = p_cols
    df = n_obs - p
    if df <= 0:
        raise ValueError(
            f"Not enough observations ({n_obs}) for the model's {p} parameters; "
            f"residual df would be {df}."
        )

    # ---- Normal equations: beta = (X'X)^-1 X'y --------------------------
    XtX = [[0.0] * p for _ in range(p)]
    XtY = [0.0] * p
    for i in range(n_obs):
        xi = X[i]
        yi = y[i]
        for a in range(p):
            xia = xi[a]
            if xia:
                XtY[a] += xia * yi
                XtX_a = XtX[a]
                for b in range(p):
                    xib = xi[b]
                    if xib:
                        XtX_a[b] += xia * xib
    XtX_inv = _mat_inv(XtX)
    beta = _matvec(XtX_inv, XtY)

    # ---- Residuals, error variance --------------------------------------
    rss = 0.0
    for i in range(n_obs):
        fitted = 0.0
        xi = X[i]
        for a in range(p):
            if xi[a]:
                fitted += xi[a] * beta[a]
        resid = y[i] - fitted
        rss += resid * resid
    mse = rss / df

    beta_T = beta[t_idx]
    se_ln = math.sqrt(mse * XtX_inv[t_idx][t_idx])

    gmr = math.exp(beta_T)
    tcrit = _t_quantile(0.95, df)                       # 90 % two-sided -> 0.95
    ci_lo_ln = beta_T - tcrit * se_ln
    ci_hi_ln = beta_T + tcrit * se_ln

    point_pct = 100.0 * gmr
    ci_lower_pct = 100.0 * math.exp(ci_lo_ln)
    ci_upper_pct = 100.0 * math.exp(ci_hi_ln)
    # Intra-subject CV% from the residual (within-subject) variance on ln scale.
    cv_intra_pct = 100.0 * math.sqrt(math.exp(mse) - 1.0)

    lower_limit, upper_limit = _NTI_LIMITS if is_nti else _ABE_LIMITS
    within = (ci_lower_pct >= lower_limit) and (ci_upper_pct <= upper_limit)

    # ---- R cross-check (best-effort; never fatal) -----------------------
    try:
        r_cross = _r_crosscheck_crossover(records)
    except Exception as exc:  # noqa: BLE001 — provenance must never break the analysis
        r_cross = {"r_available": False, "error": f"crosscheck raised: {exc}"}

    provenance: dict[str, Any] = {
        "engine": "pure-python OLS (Gauss-Jordan)",
        "n_obs": n_obs,
        "n_params": p,
        "reference_subject": subjects[0],
        "reference_period": periods[0],
        "beta_treatment_ln": beta_T,
        "mse_ln": mse,
        "t_critical_0.95": tcrit,
        "r_crosscheck": r_cross,
    }

    return BEResult(
        metric=metric_name,
        n=len(subjects),
        point_estimate_pct=point_pct,
        ci_lower_pct=ci_lower_pct,
        ci_upper_pct=ci_upper_pct,
        cv_intra_pct=cv_intra_pct,
        df=df,
        se_ln=se_ln,
        method="ABE (log-transformed ANOVA)",
        model="ln(PK) ~ subject + period + treatment (subjects fixed)",
        lower_limit_pct=lower_limit,
        upper_limit_pct=upper_limit,
        within_be_limits=within,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Parallel (two independent groups) — Welch t on the log scale
# ---------------------------------------------------------------------------


def analyze_parallel(
    records: Sequence[dict], *, metric_name: str = "", is_nti: bool = False
) -> BEResult:
    """Average bioequivalence for a parallel-group design (one value per subject).

    Test and Reference are two INDEPENDENT groups. Uses the Welch (unequal
    variance) two-sample t-test on ln(value): ``diff = mean_ln(T) - mean_ln(R)``,
    ``SE = sqrt(varT/nT + varR/nR)`` with sample variances (ddof=1), and
    Satterthwaite degrees of freedom. The 90 % CI is ``exp(diff +/- t*SE)``.
    """
    _validate_records(records, need_period=False)

    t_vals = [math.log(r["value"]) for r in records if r["treatment"] == "T"]
    r_vals = [math.log(r["value"]) for r in records if r["treatment"] == "R"]
    nT, nR = len(t_vals), len(r_vals)
    if nT < 2 or nR < 2:
        raise ValueError(
            f"Parallel design needs >= 2 subjects per arm (got T={nT}, R={nR})."
        )

    mean_T = sum(t_vals) / nT
    mean_R = sum(r_vals) / nR
    var_T = sum((x - mean_T) ** 2 for x in t_vals) / (nT - 1)   # ddof=1
    var_R = sum((x - mean_R) ** 2 for x in r_vals) / (nR - 1)   # ddof=1

    diff = mean_T - mean_R
    se_T2 = var_T / nT
    se_R2 = var_R / nR
    se_ln = math.sqrt(se_T2 + se_R2)

    # Satterthwaite (Welch) degrees of freedom.
    denom = (se_T2 ** 2) / (nT - 1) + (se_R2 ** 2) / (nR - 1)
    df_satt = (se_T2 + se_R2) ** 2 / denom

    gmr = math.exp(diff)
    tcrit = _t_quantile(0.95, df_satt)                  # 90 % two-sided
    ci_lower_pct = 100.0 * math.exp(diff - tcrit * se_ln)
    ci_upper_pct = 100.0 * math.exp(diff + tcrit * se_ln)
    point_pct = 100.0 * gmr

    # Between-subject CV% from the pooled sample variance on the ln scale
    # (descriptive only; the CI above is the Welch interval).
    pooled_var = ((nT - 1) * var_T + (nR - 1) * var_R) / (nT + nR - 2)
    cv_between_pct = 100.0 * math.sqrt(math.exp(pooled_var) - 1.0)

    lower_limit, upper_limit = _NTI_LIMITS if is_nti else _ABE_LIMITS
    within = (ci_lower_pct >= lower_limit) and (ci_upper_pct <= upper_limit)

    provenance = {
        "engine": "pure-python Welch two-sample t (ln scale)",
        "n_T": nT,
        "n_R": nR,
        "mean_ln_T": mean_T,
        "mean_ln_R": mean_R,
        "var_ln_T": var_T,
        "var_ln_R": var_R,
        "diff_ln": diff,
        "df_satterthwaite": df_satt,
        "t_critical_0.95": tcrit,
        "cv_note": "cv_intra_pct holds the BETWEEN-subject CV% for a parallel design.",
    }

    return BEResult(
        metric=metric_name,
        n=nT + nR,
        point_estimate_pct=point_pct,
        ci_lower_pct=ci_lower_pct,
        ci_upper_pct=ci_upper_pct,
        cv_intra_pct=cv_between_pct,
        df=int(round(df_satt)),           # exact fractional df in provenance
        se_ln=se_ln,
        method="ABE (log-transformed Welch two-sample t)",
        model="ln(PK) ~ treatment (two independent groups, unequal variance)",
        lower_limit_pct=lower_limit,
        upper_limit_pct=upper_limit,
        within_be_limits=within,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def analyze_be(
    records: Sequence[dict],
    design_key: str,
    *,
    metrics: Optional[Sequence[str]] = None,
    is_nti: bool = False,
) -> dict:
    """Route observed BE data to the right analyzer and return plain dicts.

    ``design_key``:
        * ``"crossover"`` -> :func:`analyze_crossover_2x2`
        * ``"parallel"``  -> :func:`analyze_parallel`
        * ``"replicate"`` -> ``NotImplementedError`` (RSABE is a later phase)

    If ``metrics`` (e.g. ``["cmax", "auc0t", "auc0inf"]``) is given, each record
    must carry a value per metric — either under ``record["values"][metric]`` or
    directly under ``record[metric]`` — and the return is
    ``{metric: BEResult.to_dict()}``. Otherwise the single ``record["value"]``
    series is analyzed and its ``BEResult.to_dict()`` is returned.
    """
    key = (design_key or "").strip().lower()
    if key == "replicate":
        raise NotImplementedError("Replicate/RSABE analysis is a later phase")
    if key == "crossover":
        analyzer = analyze_crossover_2x2
    elif key == "parallel":
        analyzer = analyze_parallel
    else:
        raise ValueError(
            f"Unknown design_key {design_key!r} (expected 'crossover', "
            f"'parallel' or 'replicate')."
        )

    if metrics:
        results: dict[str, dict] = {}
        for metric in metrics:
            per_metric_records = []
            for r in records:
                value = None
                vals = r.get("values")
                if isinstance(vals, dict) and metric in vals:
                    value = vals[metric]
                elif metric in r:
                    value = r[metric]
                if value is None:
                    raise ValueError(
                        f"Record for subject {r.get('subject')!r} is missing a "
                        f"value for metric {metric!r}."
                    )
                rr = dict(r)
                rr["value"] = value
                per_metric_records.append(rr)
            results[metric] = analyzer(
                per_metric_records, metric_name=metric, is_nti=is_nti
            ).to_dict()
        return results

    return analyzer(records, metric_name="", is_nti=is_nti).to_dict()


# ---------------------------------------------------------------------------
# R cross-check — ground truth from lm()
# ---------------------------------------------------------------------------


def _r_crosscheck_crossover(records: Sequence[dict]) -> dict:
    """Run the crossover model in R's ``lm()`` and return its GMR / 90 % CI / df.

    Builds the data frame inline, relevels ``treatment`` so ``R`` is the
    reference (making the coefficient ``factor(treatment)T`` the Test-minus-
    Reference contrast), fits the same model, and parses four numbers. Returns
    ``{"r_available": True, "gmr", "ci_lower", "ci_upper", "df"}`` (ratios, not
    percentages) or ``{"r_available": False, "error": ...}``. Never raises.
    """
    rscript = _find_rscript()
    if not rscript:
        return {"r_available": False, "error": "Rscript not found on this host."}

    subs = ",".join('"%s"' % str(r["subject"]) for r in records)
    pers = ",".join(str(int(r["period"])) for r in records)
    trts = ",".join('"%s"' % str(r["treatment"]) for r in records)
    vals = ",".join(repr(float(r["value"])) for r in records)

    body = (
        "d <- data.frame(subject=c(%s), period=c(%s), treatment=c(%s), "
        "value=c(%s), stringsAsFactors=FALSE)\n" % (subs, pers, trts, vals)
        + 'd$treatment <- relevel(factor(d$treatment), ref="R")\n'
        + "m <- lm(log(value) ~ factor(subject) + factor(period) + "
          "factor(treatment), data=d)\n"
        + 'cn <- "factor(treatment)T"\n'
        + "est <- coef(m)[[cn]]\n"
        + "ci <- confint(m, cn, level=0.90)\n"
        + 'cat(sprintf("BE_R_RESULT|%.10f|%.10f|%.10f|%d", exp(est), '
          "exp(ci[1]), exp(ci[2]), as.integer(df.residual(m))))\n"
    )
    try:
        out, err, rc = _run_r(rscript, body, timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        return {"r_available": False, "error": f"R invocation failed: {exc}"}

    m = re.search(
        r"BE_R_RESULT\|([-0-9.eE+]+)\|([-0-9.eE+]+)\|([-0-9.eE+]+)\|(-?\d+)", out
    )
    if not m:
        return {
            "r_available": False,
            "error": (out.strip()[:200] or err.strip()[:200] or "no R output"),
        }
    return {
        "r_available": True,
        "gmr": float(m.group(1)),
        "ci_lower": float(m.group(2)),
        "ci_upper": float(m.group(3)),
        "df": int(m.group(4)),
        "rscript": rscript,
    }
