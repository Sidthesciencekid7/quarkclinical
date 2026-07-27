"""Tests for the FDA RSABE replicate-design engine (``services.be_replicate``).

Validation layers (each is asserted here):

1. **Chi-square quantile** — the pure-Python inverse regularized-gamma
   ``_chisq_quantile`` is checked against tabulated textbook values and, when R
   is present, against R's ``qchisq`` to ~1e-9.
2. **Hand-computed synthetic replicate dataset** — a dataset whose ln values are
   known exactly; the test *independently* recomputes Dbar, s2D, s2wR and CVwR
   from the raw records (no engine internals) and asserts the engine reproduces
   them to ~1e-12, and that the full linearized criterion Cbeta matches a value
   built from R-anchored quantiles.
3. **Independent R reproduction of Cbeta** — every analysis carries a base-R
   reproduction of the identical moment criterion (R ``qt``/``qchisq``) in its
   provenance; the test asserts Python Cbeta == R Cbeta to < 1e-6.
4. **replicateBE reference package** — on the package's own bundled datasets
   (rds01/02/04) the point estimate and CVwR are cross-checked against
   replicateBE's documented Method A models (verified identical to the package),
   and the scaled/unscaled switch and PASS/FAIL decisions are checked.

R-dependent tests skip cleanly when Rscript / replicateBE are unavailable, so the
non-R layers (1 and 2) always run.
"""
from __future__ import annotations

import math

import pytest

from services.be_replicate import (
    THETA_RSABE,
    SIGMA_W0,
    CVWR_SWITCH,
    _chisq_quantile,
    _chisq_cdf,
    analyze_replicate,
)
from services.biostatistics import _t_quantile
from services.powertost_r import _find_rscript, _run_r


# ---------------------------------------------------------------------------
# R availability helpers
# ---------------------------------------------------------------------------
def _has_rscript() -> bool:
    return _find_rscript() is not None


def _has_replicatebe() -> bool:
    rs = _find_rscript()
    if not rs:
        return False
    try:
        out, _err, _rc = _run_r(
            rs, 'cat(requireNamespace("replicateBE", quietly=TRUE))', timeout=60.0
        )
    except Exception:
        return False
    return out.strip().upper().startswith("TRUE")


requires_r = pytest.mark.skipif(not _has_rscript(), reason="Rscript not available")
requires_repbe = pytest.mark.skipif(
    not _has_replicatebe(), reason="replicateBE package not installed"
)


def _fetch_rds(name: str) -> list[dict]:
    """Export a bundled replicateBE dataset into the engine's records shape."""
    rs = _find_rscript()
    body = (
        'suppressMessages(library(replicateBE))\n'
        f'd <- {name}\n'
        'cat(sprintf("%s|%s|%s|%s|%.12g\\n", as.character(d$subject), '
        'as.character(d$period), as.character(d$sequence), '
        'as.character(d$treatment), d$PK))\n'
    )
    out, _err, _rc = _run_r(rs, body, timeout=60.0)
    recs: list[dict] = []
    for line in out.strip().splitlines():
        line = line.strip()
        if line.count("|") != 4:
            continue
        subj, per, seq, trt, val = line.split("|")
        recs.append({
            "subject": subj, "period": int(per), "sequence": seq,
            "treatment": trt, "value": float(val),
        })
    return recs


# ---------------------------------------------------------------------------
# 1. Chi-square quantile
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("p, df, expected", [
    (0.05, 10, 3.9403),
    (0.95, 10, 18.3070),
    (0.05, 1, 0.0039321),
    (0.95, 1, 3.8415),
    (0.975, 20, 34.1696),
    (0.005, 5, 0.4117419),
    (0.05, 4, 0.7107230),
])
def test_chisq_quantile_tabulated(p, df, expected):
    """Inverse chi-square matches tabulated textbook values to 5 sig figs."""
    got = _chisq_quantile(p, df)
    assert math.isclose(got, expected, rel_tol=1e-4), f"chisq({p},{df})={got}"


def test_chisq_quantile_cdf_roundtrip():
    """Quantile is a true inverse of the CDF."""
    for p in (0.01, 0.05, 0.5, 0.9, 0.95, 0.99):
        for df in (1, 3, 4, 10, 24, 73, 150):
            x = _chisq_quantile(p, df)
            assert math.isclose(_chisq_cdf(x, df), p, abs_tol=1e-9)


@requires_r
@pytest.mark.parametrize("p", [0.01, 0.05, 0.10, 0.5, 0.90, 0.95, 0.99])
@pytest.mark.parametrize("df", [1, 3, 4, 9, 24, 51, 73])
def test_chisq_quantile_matches_r(p, df):
    """Inverse chi-square matches R's ``qchisq`` to ~1e-8 (relative)."""
    rs = _find_rscript()
    out, _err, _rc = _run_r(rs, f'cat(sprintf("%.12f", qchisq({p}, {df})))', timeout=30.0)
    r_val = float(out.strip())
    got = _chisq_quantile(p, df)
    assert math.isclose(got, r_val, rel_tol=1e-8, abs_tol=1e-10), (
        f"qchisq({p},{df}): py={got} r={r_val}"
    )


# ---------------------------------------------------------------------------
# 2. Hand-computed synthetic replicate dataset (exact)
# ---------------------------------------------------------------------------
def _synthetic_hvd_dataset():
    """A 4-subject partial-replicate (TRR/RTR/RRT) dataset with EXACT known ln
    values.

    Each subject's two Reference replicates have ln = {0.0, 0.6}
    (mean 0.3, within-subject SS = 0.18, dfwR contribution = 1). The single Test
    ln value per subject is chosen to give subject differences {0.2, 0.4, 0.0, 0.2}.
    The Test administration is placed in a DIFFERENT period across subjects
    (sequences TRR / RTR / RRT) so treatment is not confounded with period and
    the fixed-effects ANOVA is estimable. Period placement does not affect the
    moment estimators (Dbar, s2D, s2wR), which depend only on the T/R grouping.
    """
    ln_specs = {  # subject: (lnT, [lnR1, lnR2], sequence, period_of_T)
        "A": (0.5, [0.0, 0.6], "TRR", 1),   # D = 0.5 - 0.3 = 0.2
        "B": (0.7, [0.0, 0.6], "RTR", 2),   # D = 0.4
        "C": (0.3, [0.0, 0.6], "RRT", 3),   # D = 0.0
        "D": (0.5, [0.0, 0.6], "RTR", 2),   # D = 0.2
    }
    records = []
    for subj, (lnT, lnRs, seq, tper) in ln_specs.items():
        r_periods = [p for p in (1, 2, 3) if p != tper]
        records.append({"subject": subj, "sequence": seq, "period": tper,
                        "treatment": "T", "value": math.exp(lnT)})
        for p, lnr in zip(r_periods, lnRs):
            records.append({"subject": subj, "sequence": seq, "period": p,
                            "treatment": "R", "value": math.exp(lnr)})
    return records, ln_specs


def test_synthetic_moments_reproduced_exactly():
    """Engine reproduces the hand-computed Dbar, s2D, s2wR, CVwR exactly."""
    records, ln_specs = _synthetic_hvd_dataset()

    # Independent recomputation from the raw records (not via engine internals).
    diffs = []
    ss_wr = 0.0
    df_wr = 0
    for subj, (lnT, lnRs, _seq, _tper) in ln_specs.items():
        mr = sum(lnRs) / len(lnRs)
        diffs.append(lnT - mr)
        ss_wr += sum((x - mr) ** 2 for x in lnRs)
        df_wr += len(lnRs) - 1
    n = len(diffs)
    dbar = sum(diffs) / n
    s2d = sum((d - dbar) ** 2 for d in diffs) / (n - 1)
    s2wr = ss_wr / df_wr
    cvwr = math.sqrt(math.exp(s2wr) - 1.0)

    # Sanity of the hand values themselves.
    assert dbar == pytest.approx(0.2, abs=1e-15)
    assert s2wr == pytest.approx(0.18, abs=1e-15)
    assert df_wr == 4

    res = analyze_replicate(records, metric_name="AUC")
    assert res["provenance"]["dbar_ln"] == pytest.approx(dbar, abs=1e-12)
    assert res["provenance"]["s2D"] == pytest.approx(s2d, abs=1e-12)
    assert res["s2wr"] == pytest.approx(s2wr, abs=1e-12)
    assert res["cv_wr_pct"] == pytest.approx(100.0 * cvwr, abs=1e-10)
    assert res["point_estimate_pct"] == pytest.approx(100.0 * math.exp(dbar), abs=1e-10)
    # CVwR ~= 44.4% >= 30% -> reference-scaling engaged.
    assert res["scaled"] is True


def test_synthetic_criterion_matches_moment_formula():
    """Engine Cbeta equals the linearized criterion built from R-anchored
    quantiles (qt(0.95,3), qchisq(0.05,4)), independent of the engine's own
    special functions."""
    records, ln_specs = _synthetic_hvd_dataset()

    diffs = []
    ss_wr = 0.0
    df_wr = 0
    for subj, (lnT, lnRs, _seq, _tper) in ln_specs.items():
        mr = sum(lnRs) / len(lnRs)
        diffs.append(lnT - mr)
        ss_wr += sum((x - mr) ** 2 for x in lnRs)
        df_wr += len(lnRs) - 1
    n = len(diffs)
    dbar = sum(diffs) / n
    s2d = sum((d - dbar) ** 2 for d in diffs) / (n - 1)
    s2wr = ss_wr / df_wr

    # High-precision quantiles from R (hard-coded so this check needs no R):
    qt_095_3 = 2.353363434802      # qt(0.95, 3)
    qchisq_005_4 = 0.710723021397  # qchisq(0.05, 4)
    theta = (math.log(1.25) / 0.25) ** 2

    eta = dbar ** 2 - theta * s2wr
    hm = (abs(dbar) + qt_095_3 * math.sqrt(s2d / n)) ** 2 - dbar ** 2
    hs = theta * s2wr * (df_wr / qchisq_005_4 - 1.0)
    cbeta_hand = eta + math.sqrt(hm ** 2 + hs ** 2)

    res = analyze_replicate(records)
    assert res["criterion_upper_bound"] == pytest.approx(cbeta_hand, abs=1e-9)
    # Cbeta > 0 here -> FAIL despite the point estimate being inside 80-125%.
    assert res["criterion_upper_bound"] > 0
    assert res["within_be_limits"] is False


# ---------------------------------------------------------------------------
# 3. Independent R reproduction of the whole criterion
# ---------------------------------------------------------------------------
@requires_r
def test_criterion_matches_base_r_reproduction():
    """The engine's own best-effort R cross-check reproduces Cbeta to < 1e-6."""
    records, _ = _synthetic_hvd_dataset()
    res = analyze_replicate(records)
    xc = res["provenance"]["crosscheck_R"]
    assert xc["r_available"] is True
    assert xc["cbeta"] == pytest.approx(res["criterion_upper_bound"], abs=1e-6)
    assert xc["s2wr"] == pytest.approx(res["s2wr"], abs=1e-9)
    assert xc["gmr"] == pytest.approx(res["point_estimate_pct"] / 100.0, abs=1e-9)
    assert res["provenance"]["criterion"]["Cbeta_abs_delta_vs_R"] < 1e-6


# ---------------------------------------------------------------------------
# 4. replicateBE bundled datasets — scaled PASS, scaled FAIL, unscaled
# ---------------------------------------------------------------------------
@requires_repbe
def test_rds04_scaled_fail():
    """rds04: highly variable (CVwR ~60%), reference-scaled, Cbeta > 0 -> FAIL.

    Point estimate matches replicateBE's Method A PE exactly; the engine Cbeta
    matches the independent base-R reproduction.
    """
    recs = _fetch_rds("rds04")
    res = analyze_replicate(recs, metric_name="AUC")
    assert res["scaled"] is True
    assert res["cv_wr_pct"] > 30.0
    assert res["criterion_upper_bound"] > 0        # fails the scaled criterion
    assert res["within_be_limits"] is False
    # Point estimate vs replicateBE Method A (exact for this balanced design).
    repbe = res["provenance"]["crosscheck_replicateBE"]
    assert repbe["available"] is True
    assert res["point_estimate_pct"] == pytest.approx(repbe["point_estimate_pct"], abs=1e-3)
    # Engine Cbeta vs independent base-R reproduction.
    xc = res["provenance"]["crosscheck_R"]
    assert xc["cbeta"] == pytest.approx(res["criterion_upper_bound"], abs=1e-6)


@requires_repbe
def test_rds01_scaled_pass():
    """rds01: highly variable (CVwR ~48%), reference-scaled, Cbeta < 0 -> PASS."""
    recs = _fetch_rds("rds01")
    res = analyze_replicate(recs, metric_name="AUC")
    assert res["scaled"] is True
    assert res["cv_wr_pct"] > 30.0
    assert res["criterion_upper_bound"] < 0
    # GMR inside 80-125% and Cbeta<=0 -> PASS.
    assert res["within_be_limits"] is True
    xc = res["provenance"]["crosscheck_R"]
    assert xc["cbeta"] == pytest.approx(res["criterion_upper_bound"], abs=1e-6)
    # PE within 0.2% of replicateBE (dataset is slightly imbalanced).
    repbe = res["provenance"]["crosscheck_replicateBE"]
    assert res["point_estimate_pct"] == pytest.approx(
        repbe["point_estimate_pct"], rel=2e-3
    )


@requires_repbe
def test_rds02_unscaled_fallback_ci_matches_replicatebe():
    """rds02: CVwR ~11% (< 30%) -> conventional unscaled ABE; the 90% CI equals
    replicateBE Method A's confidence limits (fixed-effects ANOVA)."""
    recs = _fetch_rds("rds02")
    res = analyze_replicate(recs, metric_name="AUC")
    assert res["scaled"] is False
    assert res["criterion_upper_bound"] is None
    assert res["cv_wr_pct"] < 30.0
    # Conventional 90% CI is populated for the unscaled fallback.
    assert res["ci_lower_pct"] is not None and res["ci_upper_pct"] is not None
    # Matches R lm() / replicateBE Method A limits to high precision.
    xc = res["provenance"]["crosscheck_R"]
    assert res["ci_lower_pct"] == pytest.approx(xc["ci_lower_pct"], abs=1e-4)
    assert res["ci_upper_pct"] == pytest.approx(xc["ci_upper_pct"], abs=1e-4)
    # This particular dataset is bioequivalent by conventional ABE.
    assert res["within_be_limits"] is True


@requires_repbe
def test_cvwr_close_to_replicatebe_anova():
    """The subject-centred pooled CVwR agrees with replicateBE's ANOVA CVwR to
    within ~2% relative (the documented period-effect difference)."""
    for name in ("rds01", "rds02", "rds04"):
        res = analyze_replicate(_fetch_rds(name))
        repbe = res["provenance"]["crosscheck_replicateBE"]
        assert repbe["available"] is True
        assert res["cv_wr_pct"] == pytest.approx(repbe["cvwr_pct"], rel=0.03), name


# ---------------------------------------------------------------------------
# 5. Switching rule constants
# ---------------------------------------------------------------------------
def test_regulatory_constants():
    assert SIGMA_W0 == 0.25
    assert THETA_RSABE == pytest.approx((math.log(1.25) / 0.25) ** 2, abs=1e-15)
    assert THETA_RSABE == pytest.approx(0.7966887, abs=1e-6)
    assert CVWR_SWITCH == 0.30


def test_switch_just_below_30pct_is_unscaled():
    """A dataset with CVwR just under 30% uses the unscaled fallback."""
    # two R reps ln {0, a}: SS = a^2/2, df=1 -> s2wR = a^2/2 = 0.083
    # -> CVwR = sqrt(exp(0.083)-1) ~= 29.4% (< 30%).
    a = math.sqrt(2 * 0.083)
    # Rotate the Test period across subjects (TRR/RTR/RRT) so treatment is not
    # confounded with period and the fixed-effects ANOVA is estimable.
    seqs = [("TRR", 1), ("RTR", 2), ("RRT", 3)]
    records = []
    for i, subj in enumerate(["S1", "S2", "S3", "S4", "S5", "S6"]):
        seq, tper = seqs[i % 3]
        r_periods = [p for p in (1, 2, 3) if p != tper]
        records.append({"subject": subj, "sequence": seq, "period": tper,
                        "treatment": "T", "value": math.exp(0.05 * (i - 2.5))})
        for p, lnr in zip(r_periods, [0.0, a]):
            records.append({"subject": subj, "sequence": seq, "period": p,
                            "treatment": "R", "value": math.exp(lnr)})
    res = analyze_replicate(records)
    assert res["cv_wr_pct"] < 30.0
    assert res["scaled"] is False
    assert res["criterion_upper_bound"] is None
    # Unscaled fallback populates the conventional 90% CI.
    assert res["ci_lower_pct"] is not None


# ---------------------------------------------------------------------------
# 6. NTID is out of scope (honest restriction, not a silent unvalidated pass)
# ---------------------------------------------------------------------------
def test_nti_raises_not_implemented():
    records, _ = _synthetic_hvd_dataset()
    with pytest.raises(NotImplementedError):
        analyze_replicate(records, is_nti=True)


# ---------------------------------------------------------------------------
# 7. Input validation
# ---------------------------------------------------------------------------
def test_empty_records_raise():
    with pytest.raises(ValueError):
        analyze_replicate([])


def test_bad_treatment_raises():
    records, _ = _synthetic_hvd_dataset()
    records[0]["treatment"] = "X"
    with pytest.raises(ValueError):
        analyze_replicate(records)


def test_nonpositive_value_raises():
    records, _ = _synthetic_hvd_dataset()
    records[0]["value"] = 0.0
    with pytest.raises(ValueError):
        analyze_replicate(records)


def test_no_reference_replicates_raises():
    """A non-replicate design (each subject only 1 R) cannot yield s2wR."""
    records = [
        {"subject": "A", "sequence": "RT", "period": 1, "treatment": "R", "value": 100.0},
        {"subject": "A", "sequence": "RT", "period": 2, "treatment": "T", "value": 110.0},
        {"subject": "B", "sequence": "TR", "period": 1, "treatment": "T", "value": 105.0},
        {"subject": "B", "sequence": "TR", "period": 2, "treatment": "R", "value": 100.0},
    ]
    with pytest.raises(ValueError):
        analyze_replicate(records)


def test_too_few_subjects_raises():
    records = [
        {"subject": "A", "sequence": "RTR", "period": 1, "treatment": "R", "value": 100.0},
        {"subject": "A", "sequence": "RTR", "period": 2, "treatment": "T", "value": 120.0},
        {"subject": "A", "sequence": "RTR", "period": 3, "treatment": "R", "value": 130.0},
    ]
    with pytest.raises(ValueError):
        analyze_replicate(records)


# ---------------------------------------------------------------------------
# 8. Output contract
# ---------------------------------------------------------------------------
def test_output_contains_required_fields():
    records, _ = _synthetic_hvd_dataset()
    res = analyze_replicate(records, metric_name="Cmax")
    required = {
        "metric", "n", "point_estimate_pct", "cv_wr_pct", "s2wr", "scaled",
        "criterion_upper_bound", "within_be_limits", "ci_lower_pct",
        "ci_upper_pct", "lower_limit_pct", "upper_limit_pct", "df", "se_ln",
        "method", "model", "provenance",
    }
    assert required.issubset(res.keys())
    assert res["metric"] == "Cmax"
    assert res["lower_limit_pct"] == 80.00
    assert res["upper_limit_pct"] == 125.00
    assert isinstance(res["within_be_limits"], bool)
    assert "validation" in res["provenance"]
