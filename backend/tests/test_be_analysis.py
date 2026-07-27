"""Validation tests for the bioequivalence analysis engine (services.be_analysis).

The 90 % CI this engine produces is the pivotal number of a bioequivalence
submission, so it is validated against **R's ``lm()``** as ground truth. The
canonical crossover dataset below was run through R once (via
``_r_crosscheck_crossover``); the resulting GMR / 90 % CI / df are hard-coded as
the expected values (see the ``R_*`` constants, each flagged
"ground truth from R lm()"). A second test re-runs R live, when available, and
asserts the pure-Python engine reproduces it.

To (re)generate the ground-truth numbers::

    python -m pytest tests/test_be_analysis.py -s -k print_ground_truth
"""
from __future__ import annotations

import math

import pytest

from services.be_analysis import (
    BEResult,
    analyze_be,
    analyze_crossover_2x2,
    analyze_parallel,
    _r_crosscheck_crossover,
)
from services.powertost_r import _find_rscript


# ---------------------------------------------------------------------------
# Canonical 2x2 crossover dataset (24 subjects, 48 obs, realistic Cmax ng/mL).
# Sequence TR: period 1 = Test, period 2 = Reference.
# Sequence RT: period 1 = Reference, period 2 = Test.
# ---------------------------------------------------------------------------
CANONICAL = [
    {"subject": "S01", "sequence": "TR", "period": 1, "treatment": "T", "value": 115.27},
    {"subject": "S01", "sequence": "TR", "period": 2, "treatment": "R", "value": 119.40},
    {"subject": "S02", "sequence": "TR", "period": 1, "treatment": "T", "value": 89.64},
    {"subject": "S02", "sequence": "TR", "period": 2, "treatment": "R", "value": 104.79},
    {"subject": "S03", "sequence": "TR", "period": 1, "treatment": "T", "value": 129.65},
    {"subject": "S03", "sequence": "TR", "period": 2, "treatment": "R", "value": 129.41},
    {"subject": "S04", "sequence": "TR", "period": 1, "treatment": "T", "value": 77.55},
    {"subject": "S04", "sequence": "TR", "period": 2, "treatment": "R", "value": 94.64},
    {"subject": "S05", "sequence": "TR", "period": 1, "treatment": "T", "value": 108.77},
    {"subject": "S05", "sequence": "TR", "period": 2, "treatment": "R", "value": 100.77},
    {"subject": "S06", "sequence": "TR", "period": 1, "treatment": "T", "value": 108.11},
    {"subject": "S06", "sequence": "TR", "period": 2, "treatment": "R", "value": 126.51},
    {"subject": "S07", "sequence": "TR", "period": 1, "treatment": "T", "value": 93.69},
    {"subject": "S07", "sequence": "TR", "period": 2, "treatment": "R", "value": 89.44},
    {"subject": "S08", "sequence": "TR", "period": 1, "treatment": "T", "value": 140.65},
    {"subject": "S08", "sequence": "TR", "period": 2, "treatment": "R", "value": 140.48},
    {"subject": "S09", "sequence": "TR", "period": 1, "treatment": "T", "value": 100.25},
    {"subject": "S09", "sequence": "TR", "period": 2, "treatment": "R", "value": 114.23},
    {"subject": "S10", "sequence": "TR", "period": 1, "treatment": "T", "value": 105.45},
    {"subject": "S10", "sequence": "TR", "period": 2, "treatment": "R", "value": 123.31},
    {"subject": "S11", "sequence": "TR", "period": 1, "treatment": "T", "value": 91.88},
    {"subject": "S11", "sequence": "TR", "period": 2, "treatment": "R", "value": 87.82},
    {"subject": "S12", "sequence": "TR", "period": 1, "treatment": "T", "value": 115.52},
    {"subject": "S12", "sequence": "TR", "period": 2, "treatment": "R", "value": 134.94},
    {"subject": "S13", "sequence": "RT", "period": 1, "treatment": "R", "value": 111.70},
    {"subject": "S13", "sequence": "RT", "period": 2, "treatment": "T", "value": 90.62},
    {"subject": "S14", "sequence": "RT", "period": 1, "treatment": "R", "value": 107.37},
    {"subject": "S14", "sequence": "RT", "period": 2, "treatment": "T", "value": 118.65},
    {"subject": "S15", "sequence": "RT", "period": 1, "treatment": "R", "value": 87.80},
    {"subject": "S15", "sequence": "RT", "period": 2, "treatment": "T", "value": 92.52},
    {"subject": "S16", "sequence": "RT", "period": 1, "treatment": "R", "value": 138.04},
    {"subject": "S16", "sequence": "RT", "period": 2, "treatment": "T", "value": 141.51},
    {"subject": "S17", "sequence": "RT", "period": 1, "treatment": "R", "value": 91.88},
    {"subject": "S17", "sequence": "RT", "period": 2, "treatment": "T", "value": 101.29},
    {"subject": "S18", "sequence": "RT", "period": 1, "treatment": "R", "value": 108.70},
    {"subject": "S18", "sequence": "RT", "period": 2, "treatment": "T", "value": 101.22},
    {"subject": "S19", "sequence": "RT", "period": 1, "treatment": "R", "value": 128.42},
    {"subject": "S19", "sequence": "RT", "period": 2, "treatment": "T", "value": 115.38},
    {"subject": "S20", "sequence": "RT", "period": 1, "treatment": "R", "value": 79.44},
    {"subject": "S20", "sequence": "RT", "period": 2, "treatment": "T", "value": 87.53},
    {"subject": "S21", "sequence": "RT", "period": 1, "treatment": "R", "value": 117.44},
    {"subject": "S21", "sequence": "RT", "period": 2, "treatment": "T", "value": 119.71},
    {"subject": "S22", "sequence": "RT", "period": 1, "treatment": "R", "value": 114.19},
    {"subject": "S22", "sequence": "RT", "period": 2, "treatment": "T", "value": 96.17},
    {"subject": "S23", "sequence": "RT", "period": 1, "treatment": "R", "value": 90.78},
    {"subject": "S23", "sequence": "RT", "period": 2, "treatment": "T", "value": 84.39},
    {"subject": "S24", "sequence": "RT", "period": 1, "treatment": "R", "value": 140.22},
    {"subject": "S24", "sequence": "RT", "period": 2, "treatment": "T", "value": 130.76},
]

# --- ground truth from R lm() (relevel treatment ref="R") -------------------
# R:  lm(log(value) ~ factor(subject) + factor(period) + factor(treatment))
#     exp(coef["factor(treatment)T"])  and  confint(..., level=0.90)
# These are % of Reference (100 * GMR / CI ratios).
R_POINT_PCT = 95.39923   # ground truth from R lm()  (GMR = 0.9539923)
R_CI_LOWER_PCT = 92.00988  # ground truth from R lm()  (0.9200988)
R_CI_UPPER_PCT = 98.91342  # ground truth from R lm()  (0.9891342)
R_DF = 22                  # ground truth from R lm()  df.residual

R_AVAILABLE = _find_rscript() is not None


# ---------------------------------------------------------------------------
# Parallel-group dataset (nT=10, nR=11) and its R ground truth.
# ---------------------------------------------------------------------------
PARALLEL = [
    {"subject": "P01", "treatment": "T", "value": 102.4},
    {"subject": "P02", "treatment": "T", "value": 95.1},
    {"subject": "P03", "treatment": "T", "value": 110.8},
    {"subject": "P04", "treatment": "T", "value": 88.6},
    {"subject": "P05", "treatment": "T", "value": 121.3},
    {"subject": "P06", "treatment": "T", "value": 99.9},
    {"subject": "P07", "treatment": "T", "value": 106.7},
    {"subject": "P08", "treatment": "T", "value": 93.2},
    {"subject": "P09", "treatment": "T", "value": 115.5},
    {"subject": "P10", "treatment": "T", "value": 100.1},
    {"subject": "P11", "treatment": "R", "value": 108.2},
    {"subject": "P12", "treatment": "R", "value": 112.9},
    {"subject": "P13", "treatment": "R", "value": 97.4},
    {"subject": "P14", "treatment": "R", "value": 125.6},
    {"subject": "P15", "treatment": "R", "value": 103.3},
    {"subject": "P16", "treatment": "R", "value": 118.7},
    {"subject": "P17", "treatment": "R", "value": 92.8},
    {"subject": "P18", "treatment": "R", "value": 130.1},
    {"subject": "P19", "treatment": "R", "value": 106.0},
    {"subject": "P20", "treatment": "R", "value": 99.5},
    {"subject": "P21", "treatment": "R", "value": 114.4},
]
# --- ground truth from R: t.test(log(T), log(R), var.equal=FALSE, conf=0.90) -
R_PAR_POINT_PCT = 94.12044     # ground truth from R  (exp(diff))
R_PAR_CI_LOWER_PCT = 87.1258   # ground truth from R
R_PAR_CI_UPPER_PCT = 101.6766  # ground truth from R
R_PAR_DF_SATT = 18.98818       # ground truth from R  (Welch/Satterthwaite df)
R_PAR_DIFF_LN = -0.06059499    # ground truth from R  (mean_ln(T) - mean_ln(R))
R_PAR_SE_LN = 0.04465805       # ground truth from R


# ---------------------------------------------------------------------------
# Clearly-BE and clearly-NOT-BE crossover datasets (12 subjects each).
# GMR engineered to ~100 % and ~70 % respectively.
# ---------------------------------------------------------------------------
CLEARLY_BE = [
    {"subject": "B01", "sequence": "TR", "period": 1, "treatment": "T", "value": 97.00},
    {"subject": "B01", "sequence": "TR", "period": 2, "treatment": "R", "value": 104.54},
    {"subject": "B02", "sequence": "TR", "period": 1, "treatment": "T", "value": 118.80},
    {"subject": "B02", "sequence": "TR", "period": 2, "treatment": "R", "value": 124.24},
    {"subject": "B03", "sequence": "TR", "period": 1, "treatment": "T", "value": 88.20},
    {"subject": "B03", "sequence": "TR", "period": 2, "treatment": "R", "value": 92.26},
    {"subject": "B04", "sequence": "TR", "period": 1, "treatment": "T", "value": 110.00},
    {"subject": "B04", "sequence": "TR", "period": 2, "treatment": "R", "value": 107.18},
    {"subject": "B05", "sequence": "TR", "period": 1, "treatment": "T", "value": 135.20},
    {"subject": "B05", "sequence": "TR", "period": 2, "treatment": "R", "value": 129.97},
    {"subject": "B06", "sequence": "TR", "period": 1, "treatment": "T", "value": 96.42},
    {"subject": "B06", "sequence": "TR", "period": 2, "treatment": "R", "value": 94.01},
    {"subject": "B07", "sequence": "RT", "period": 1, "treatment": "R", "value": 101.85},
    {"subject": "B07", "sequence": "RT", "period": 2, "treatment": "T", "value": 109.77},
    {"subject": "B08", "sequence": "RT", "period": 1, "treatment": "R", "value": 113.85},
    {"subject": "B08", "sequence": "RT", "period": 2, "treatment": "T", "value": 119.06},
    {"subject": "B09", "sequence": "RT", "period": 1, "treatment": "R", "value": 86.24},
    {"subject": "B09", "sequence": "RT", "period": 2, "treatment": "T", "value": 90.21},
    {"subject": "B10", "sequence": "RT", "period": 1, "treatment": "R", "value": 125.00},
    {"subject": "B10", "sequence": "RT", "period": 2, "treatment": "T", "value": 121.80},
    {"subject": "B11", "sequence": "RT", "period": 1, "treatment": "R", "value": 101.92},
    {"subject": "B11", "sequence": "RT", "period": 2, "treatment": "T", "value": 97.98},
    {"subject": "B12", "sequence": "RT", "period": 1, "treatment": "R", "value": 113.68},
    {"subject": "B12", "sequence": "RT", "period": 2, "treatment": "T", "value": 110.84},
]

CLEARLY_NOT = [
    {"subject": "B01", "sequence": "TR", "period": 1, "treatment": "T", "value": 67.90},
    {"subject": "B01", "sequence": "TR", "period": 2, "treatment": "R", "value": 104.54},
    {"subject": "B02", "sequence": "TR", "period": 1, "treatment": "T", "value": 83.16},
    {"subject": "B02", "sequence": "TR", "period": 2, "treatment": "R", "value": 124.24},
    {"subject": "B03", "sequence": "TR", "period": 1, "treatment": "T", "value": 61.74},
    {"subject": "B03", "sequence": "TR", "period": 2, "treatment": "R", "value": 92.26},
    {"subject": "B04", "sequence": "TR", "period": 1, "treatment": "T", "value": 77.00},
    {"subject": "B04", "sequence": "TR", "period": 2, "treatment": "R", "value": 107.18},
    {"subject": "B05", "sequence": "TR", "period": 1, "treatment": "T", "value": 94.64},
    {"subject": "B05", "sequence": "TR", "period": 2, "treatment": "R", "value": 129.97},
    {"subject": "B06", "sequence": "TR", "period": 1, "treatment": "T", "value": 67.50},
    {"subject": "B06", "sequence": "TR", "period": 2, "treatment": "R", "value": 94.01},
    {"subject": "B07", "sequence": "RT", "period": 1, "treatment": "R", "value": 101.85},
    {"subject": "B07", "sequence": "RT", "period": 2, "treatment": "T", "value": 76.84},
    {"subject": "B08", "sequence": "RT", "period": 1, "treatment": "R", "value": 113.85},
    {"subject": "B08", "sequence": "RT", "period": 2, "treatment": "T", "value": 83.34},
    {"subject": "B09", "sequence": "RT", "period": 1, "treatment": "R", "value": 86.24},
    {"subject": "B09", "sequence": "RT", "period": 2, "treatment": "T", "value": 63.15},
    {"subject": "B10", "sequence": "RT", "period": 1, "treatment": "R", "value": 125.00},
    {"subject": "B10", "sequence": "RT", "period": 2, "treatment": "T", "value": 85.26},
    {"subject": "B11", "sequence": "RT", "period": 1, "treatment": "R", "value": 101.92},
    {"subject": "B11", "sequence": "RT", "period": 2, "treatment": "T", "value": 68.58},
    {"subject": "B12", "sequence": "RT", "period": 1, "treatment": "R", "value": 113.68},
    {"subject": "B12", "sequence": "RT", "period": 2, "treatment": "T", "value": 77.59},
]


# ---------------------------------------------------------------------------
# Optional helper: regenerate & print ground truth (run with -s to see it).
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not R_AVAILABLE, reason="R not available")
def test_print_ground_truth(capsys):
    """Re-derive the crossover ground truth from R and print it (informational)."""
    r = _r_crosscheck_crossover(CANONICAL)
    assert r["r_available"], r
    with capsys.disabled():
        print("\n[R lm() ground truth for CANONICAL]")
        print(f"  point_estimate_pct = {100 * r['gmr']:.5f}")
        print(f"  ci_lower_pct       = {100 * r['ci_lower']:.5f}")
        print(f"  ci_upper_pct       = {100 * r['ci_upper']:.5f}")
        print(f"  df                 = {r['df']}")


# ---------------------------------------------------------------------------
# 1. Canonical crossover vs hard-coded R ground truth.
# ---------------------------------------------------------------------------
def test_crossover_matches_r_ground_truth():
    res = analyze_crossover_2x2(CANONICAL, metric_name="cmax")

    assert isinstance(res, BEResult)
    # Point estimate within 0.1 (% of Reference) of R's GMR.
    assert abs(res.point_estimate_pct - R_POINT_PCT) < 0.1, (
        res.point_estimate_pct, R_POINT_PCT)
    # Each CI bound within 0.1 percentage points of R.
    assert abs(res.ci_lower_pct - R_CI_LOWER_PCT) < 0.1, (
        res.ci_lower_pct, R_CI_LOWER_PCT)
    assert abs(res.ci_upper_pct - R_CI_UPPER_PCT) < 0.1, (
        res.ci_upper_pct, R_CI_UPPER_PCT)
    # Degrees of freedom must match R exactly.
    assert res.df == R_DF

    # Structural sanity.
    assert res.n == 24
    assert res.provenance["n_obs"] == 48
    assert res.provenance["n_params"] == 26
    assert res.df == res.provenance["n_obs"] - res.provenance["n_params"]
    assert res.method == "ABE (log-transformed ANOVA)"
    assert res.lower_limit_pct == 80.00 and res.upper_limit_pct == 125.00
    assert res.within_be_limits is True
    assert 0.0 < res.cv_intra_pct < 100.0
    assert res.se_ln > 0.0


# ---------------------------------------------------------------------------
# 2. Live cross-check: pure-Python engine reproduces R lm() to tight tolerance.
# ---------------------------------------------------------------------------
def test_crossover_live_r_crosscheck():
    if not R_AVAILABLE:
        pytest.skip("R not available")

    res = analyze_crossover_2x2(CANONICAL, metric_name="cmax")
    rc = res.provenance["r_crosscheck"]
    assert rc.get("r_available"), rc

    # The engine and R must agree far tighter than the 0.1 pp acceptance bar.
    assert abs(res.point_estimate_pct - 100 * rc["gmr"]) < 1e-2
    assert abs(res.ci_lower_pct - 100 * rc["ci_lower"]) < 1e-2
    assert abs(res.ci_upper_pct - 100 * rc["ci_upper"]) < 1e-2
    assert res.df == rc["df"]

    # A direct call to the R bridge must agree with the engine too.
    direct = _r_crosscheck_crossover(CANONICAL)
    assert direct["r_available"]
    assert abs(res.point_estimate_pct / 100.0 - direct["gmr"]) < 1e-6
    assert abs(res.ci_lower_pct / 100.0 - direct["ci_lower"]) < 1e-6
    assert abs(res.ci_upper_pct / 100.0 - direct["ci_upper"]) < 1e-6


# ---------------------------------------------------------------------------
# 3. Parallel design vs R (Welch t on log scale).
# ---------------------------------------------------------------------------
def test_parallel_matches_r():
    res = analyze_parallel(PARALLEL, metric_name="cmax")

    assert abs(res.point_estimate_pct - R_PAR_POINT_PCT) < 0.1
    assert abs(res.ci_lower_pct - R_PAR_CI_LOWER_PCT) < 0.1
    assert abs(res.ci_upper_pct - R_PAR_CI_UPPER_PCT) < 0.1
    # Satterthwaite df: reported (rounded) field + exact value in provenance.
    assert res.df == round(R_PAR_DF_SATT)
    assert abs(res.provenance["df_satterthwaite"] - R_PAR_DF_SATT) < 1e-3
    assert abs(res.provenance["diff_ln"] - R_PAR_DIFF_LN) < 1e-5
    assert abs(res.se_ln - R_PAR_SE_LN) < 1e-5
    assert res.n == 21
    assert res.method == "ABE (log-transformed Welch two-sample t)"
    # All CI values must be finite.
    assert all(math.isfinite(v) for v in
               (res.point_estimate_pct, res.ci_lower_pct, res.ci_upper_pct))


# ---------------------------------------------------------------------------
# 4. Unbalanced crossover: drop one subject's period-2 row.
# ---------------------------------------------------------------------------
def test_unbalanced_crossover_still_computes():
    unbalanced = [r for r in CANONICAL
                  if not (r["subject"] == "S05" and r["period"] == 2)]
    assert len(unbalanced) == 47  # one row dropped

    res = analyze_crossover_2x2(unbalanced, metric_name="cmax")

    # CI must be finite and properly ordered despite the imbalance.
    assert math.isfinite(res.point_estimate_pct)
    assert math.isfinite(res.ci_lower_pct)
    assert math.isfinite(res.ci_upper_pct)
    assert res.ci_lower_pct < res.point_estimate_pct < res.ci_upper_pct

    # df = n_obs - p exactly (47 - 26 = 21).
    assert res.provenance["n_obs"] == 47
    assert res.provenance["n_params"] == 26
    assert res.df == res.provenance["n_obs"] - res.provenance["n_params"]
    assert res.df == 21

    # And it must still agree with R lm() on the unbalanced data, if available.
    if R_AVAILABLE:
        rc = res.provenance["r_crosscheck"]
        assert rc.get("r_available"), rc
        assert abs(res.point_estimate_pct - 100 * rc["gmr"]) < 1e-2
        assert abs(res.ci_lower_pct - 100 * rc["ci_lower"]) < 1e-2
        assert abs(res.ci_upper_pct - 100 * rc["ci_upper"]) < 1e-2
        assert res.df == rc["df"]


# ---------------------------------------------------------------------------
# 5. BE pass/fail decision.
# ---------------------------------------------------------------------------
def test_clearly_bioequivalent_passes():
    res = analyze_crossover_2x2(CLEARLY_BE, metric_name="cmax")
    assert res.within_be_limits is True
    assert 80.0 <= res.ci_lower_pct and res.ci_upper_pct <= 125.0
    # Engineered to sit right around 100 %.
    assert 95.0 < res.point_estimate_pct < 105.0


def test_clearly_not_bioequivalent_fails():
    res = analyze_crossover_2x2(CLEARLY_NOT, metric_name="cmax")
    assert res.within_be_limits is False
    # GMR ~70 %, whole CI below the 80 % lower limit.
    assert res.point_estimate_pct < 80.0
    assert res.ci_upper_pct < 80.0


# ---------------------------------------------------------------------------
# 6. Dispatcher: routing, per-metric, and the replicate NotImplementedError.
# ---------------------------------------------------------------------------
def test_dispatcher_single_series_crossover():
    out = analyze_be(CANONICAL, "crossover")
    assert isinstance(out, dict)
    assert abs(out["point_estimate_pct"] - R_POINT_PCT) < 0.1
    assert out["df"] == R_DF


def test_dispatcher_parallel_routing():
    out = analyze_be(PARALLEL, "parallel")
    assert isinstance(out, dict)
    assert abs(out["point_estimate_pct"] - R_PAR_POINT_PCT) < 0.1


def test_dispatcher_replicate_not_implemented():
    with pytest.raises(NotImplementedError):
        analyze_be(CANONICAL, "replicate")


def test_dispatcher_unknown_design_raises():
    with pytest.raises(ValueError):
        analyze_be(CANONICAL, "banana")


def test_dispatcher_per_metric():
    # Records carrying multiple PK metrics under a "values" dict.
    multi = []
    for r in CANONICAL:
        rr = {k: r[k] for k in ("subject", "sequence", "period", "treatment")}
        rr["values"] = {"cmax": r["value"], "auc0t": r["value"] * 8.0}
        multi.append(rr)

    out = analyze_be(multi, "crossover", metrics=["cmax", "auc0t"])
    assert set(out.keys()) == {"cmax", "auc0t"}
    # cmax must reproduce the single-series canonical result.
    assert abs(out["cmax"]["point_estimate_pct"] - R_POINT_PCT) < 0.1
    # auc0t is a pure rescaling of cmax -> identical GMR / CI (ratio invariant).
    assert abs(out["auc0t"]["point_estimate_pct"]
               - out["cmax"]["point_estimate_pct"]) < 1e-6
    assert out["cmax"]["metric"] == "cmax"
    assert out["auc0t"]["metric"] == "auc0t"


# ---------------------------------------------------------------------------
# 7. Input validation guards.
# ---------------------------------------------------------------------------
def test_rejects_nonpositive_value():
    bad = [dict(r) for r in CANONICAL]
    bad[0]["value"] = 0.0
    with pytest.raises(ValueError):
        analyze_crossover_2x2(bad)


def test_rejects_bad_treatment_label():
    bad = [dict(r) for r in CANONICAL]
    bad[0]["treatment"] = "X"
    with pytest.raises(ValueError):
        analyze_crossover_2x2(bad)
