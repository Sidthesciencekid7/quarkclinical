"""
Closed-form validation of the non-compartmental analysis engine
(``services/nca.py``).

Every expected number here is either an exact analytical result (e.g. the
AUC0-inf of a mono-exponential is C0/k) or hand-computed inside the test from
the log-down trapezoid formula — never a value scraped from software output.
These are pure-math unit tests: no database, no fixtures, no network.
"""
import math

import pytest

from services.nca import NCAResult, compute_nca, compute_profiles

LN2 = math.log(2.0)


# ---------------------------------------------------------------------------
# 1. Mono-exponential terminal phase — lambda_z must recover k exactly
# ---------------------------------------------------------------------------
def test_monoexponential_terminal_lambda_z_exact():
    """Absorption rise, then a terminal phase sampled exactly on C0*exp(-k t).

    Because the terminal points are perfectly log-linear, lambda_z must equal k
    to within floating-point error, t-half = ln2/k, R^2 = 1, the intercept =
    ln(C0), and the ARS tie rule (all windows fit perfectly) selects ALL
    terminal points.
    """
    C0, k = 100.0, 0.2
    # Absorption rise (leading BLQ at t=0), peak Cmax=70 at t=1, then a terminal
    # phase lying exactly on 100*exp(-0.2 t) at t = 2,3,4,6,8,12.
    profile = [
        (0.0, None),   # leading BLQ -> 0
        (0.25, 25.0),
        (0.5, 45.0),
        (1.0, 70.0),   # Cmax / Tmax (excluded from lambda_z)
        (2.0, C0 * math.exp(-k * 2.0)),
        (3.0, C0 * math.exp(-k * 3.0)),
        (4.0, C0 * math.exp(-k * 4.0)),
        (6.0, C0 * math.exp(-k * 6.0)),
        (8.0, C0 * math.exp(-k * 8.0)),
        (12.0, C0 * math.exp(-k * 12.0)),
    ]
    r = compute_nca(profile, lloq=0.01)

    assert r.cmax == 70.0
    assert r.tmax == 1.0
    assert r.lambda_z == pytest.approx(k, rel=1e-9)
    assert r.thalf == pytest.approx(LN2 / k, rel=1e-9)
    assert r.lambda_z_r2 == pytest.approx(1.0, abs=1e-12)
    assert r.lambda_z_r2adj == pytest.approx(1.0, abs=1e-12)
    assert r.lambda_z_intercept == pytest.approx(math.log(C0), rel=1e-9)
    # perfect fit + ARS tie rule -> keep every terminal point (6 of them)
    assert r.lambda_z_n_points == 6
    assert r.auc0t_method == "linear-up/log-down"


# ---------------------------------------------------------------------------
# 1b. Pure mono-exponential from t=0 — AUC0-inf is exactly C0/k
# ---------------------------------------------------------------------------
def test_pure_monoexponential_auc0inf_equals_C0_over_k():
    """For C(t) = C0*exp(-k t) sampled from t=0, the log-down trapezoids
    telescope: AUC0-t = (C0 - Clast)/k, hence AUC0-inf = C0/k exactly — the
    textbook analytical result — independent of the (uneven) sample grid.
    """
    C0, k = 100.0, 0.2
    times = [0.0, 1.0, 2.0, 4.0, 8.0, 12.0, 24.0]
    profile = [(t, C0 * math.exp(-k * t)) for t in times]
    r = compute_nca(profile, lloq=0.01)

    clast_exact = C0 * math.exp(-k * 24.0)
    auc0t_exact = (C0 - clast_exact) / k          # telescoped log-down sum
    auc0inf_exact = C0 / k                        # analytical AUC0-inf

    assert r.cmax == C0
    assert r.tmax == 0.0
    assert r.clast == pytest.approx(clast_exact, rel=1e-12)
    assert r.tlast == 24.0
    assert r.lambda_z == pytest.approx(k, rel=1e-9)
    assert r.auc0t == pytest.approx(auc0t_exact, rel=1e-9)
    assert r.auc0inf == pytest.approx(auc0inf_exact, rel=1e-9)
    # %extrap = Clast/C0 for a pure decay starting at t=0
    assert r.pct_extrap == pytest.approx(100.0 * clast_exact / C0, rel=1e-9)
    assert r.lambda_z_intercept == pytest.approx(math.log(C0), rel=1e-9)


# ---------------------------------------------------------------------------
# 2. Log-down trapezoid — hand-computed single interval
# ---------------------------------------------------------------------------
def test_log_down_trapezoid_single_interval_hand_computed():
    """One strictly-decreasing interval (0,10)->(2,5).

    Expected log-trapezoid area = (t2-t1)*(c1-c2)/ln(c1/c2)
                                = 2*(10-5)/ln(10/5) = 10/ln2.
    """
    r = compute_nca([(0.0, 10.0), (2.0, 5.0)], lloq=0.01)
    expected = 10.0 / LN2
    assert r.auc0t == pytest.approx(expected, abs=1e-9)
    # only 2 points, all before/at Tmax -> lambda_z cannot be estimated
    assert r.lambda_z is None
    assert "lambda_z not estimable" in r.flags


def test_linear_up_trapezoid_hand_computed():
    """Mixed rising (linear-up) then falling (log-down) profile, hand-summed.

    (0,0)->(1,10):   linear  0.5*(0+10)*1        = 5.0
    (1,10)->(2,20):  linear  0.5*(10+20)*1       = 15.0   (rising -> linear)
    (2,20)->(4,5):   log     2*(20-5)/ln(20/5)   = 30/ln4
    """
    profile = [(0.0, None), (1.0, 10.0), (2.0, 20.0), (4.0, 5.0)]
    r = compute_nca(profile, lloq=0.01)
    expected = 5.0 + 15.0 + 30.0 / math.log(4.0)
    assert r.auc0t == pytest.approx(expected, abs=1e-9)
    assert r.cmax == 20.0
    assert r.tmax == 2.0


# ---------------------------------------------------------------------------
# 3. Cmax / Tmax / Clast / Tlast — exact, including the first-time tie rule
# ---------------------------------------------------------------------------
def test_cmax_tmax_clast_tlast_exact():
    """Plateau at the maximum: Tmax must be the FIRST time the max occurs."""
    profile = [
        (0.0, None),    # leading BLQ
        (0.5, 20.0),
        (1.0, 55.0),    # first occurrence of the maximum
        (2.0, 55.0),    # plateau — must NOT move Tmax
        (4.0, 30.0),
        (8.0, 12.0),    # Clast / Tlast
    ]
    r = compute_nca(profile, lloq=0.01)
    assert r.cmax == 55.0
    assert r.tmax == 1.0
    assert r.clast == 12.0
    assert r.tlast == 8.0


# ---------------------------------------------------------------------------
# 4. BLQ handling — leading -> 0, embedded -> dropped (missing)
# ---------------------------------------------------------------------------
def test_blq_embedded_is_dropped_not_zeroed():
    """An embedded BLQ must be dropped: AUC equals the AUC of the same profile
    with that sample simply removed (NOT set to zero).  A leading BLQ instead
    contributes the (0,0) start point.
    """
    with_embedded = [(0.0, None), (1.0, 10.0), (2.0, None), (3.0, 5.0), (4.0, 2.0)]
    removed = [(0.0, None), (1.0, 10.0), (3.0, 5.0), (4.0, 2.0)]

    r_emb = compute_nca(with_embedded, lloq=0.01)
    r_rem = compute_nca(removed, lloq=0.01)

    # embedded BLQ dropped == point removed entirely
    assert r_emb.auc0t == r_rem.auc0t
    assert any("embedded BLQ" in f for f in r_emb.flags)

    # Hand value: (0,0)->(1,10) linear 5.0 ; (1,10)->(3,5) log 10/ln2 ;
    #             (3,5)->(4,2) log 3/ln(2.5)
    expected = 5.0 + 10.0 / LN2 + 3.0 / math.log(2.5)
    assert r_emb.auc0t == pytest.approx(expected, abs=1e-9)


def test_blq_leading_contributes_zero_start_point():
    """A leading BLQ at t=0 adds exactly the (0,0)->(first measurable) trapezoid;
    dropping it makes integration start at the first available point instead.
    """
    with_leading = [(0.0, None), (1.0, 10.0), (3.0, 5.0), (4.0, 2.0)]
    no_zero = [(1.0, 10.0), (3.0, 5.0), (4.0, 2.0)]

    r_lead = compute_nca(with_leading, lloq=0.01)
    r_none = compute_nca(no_zero, lloq=0.01)

    # (0,0)->(1,10) linear = 0.5*(0+10)*1 = 5.0
    assert r_lead.auc0t - r_none.auc0t == pytest.approx(5.0, abs=1e-9)


def test_all_blq_returns_none_with_flag():
    r = compute_nca([(0.0, None), (1.0, None), (2.0, None)], lloq=0.01)
    assert r.cmax is None and r.auc0t is None and r.lambda_z is None
    assert "no measurable concentrations" in r.flags


# ---------------------------------------------------------------------------
# 5. %AUCextrap — hand-verifiable Clast / lambda_z
# ---------------------------------------------------------------------------
def test_pct_extrap_exactly_12_5_percent():
    """Concentration halves every hour from 8 -> lambda_z = ln2, Clast=1.

    AUC0-inf = 8/ln2, extrapolated tail = Clast/lambda_z = 1/ln2, so
    %extrap = (1/ln2)/(8/ln2) = 1/8 = 12.5% exactly (independent of ln2).
    """
    r = compute_nca([(0.0, 8.0), (1.0, 4.0), (2.0, 2.0), (3.0, 1.0)], lloq=0.01)

    assert r.lambda_z == pytest.approx(LN2, rel=1e-9)
    assert r.clast == 1.0
    assert r.tlast == 3.0
    assert r.auc0t == pytest.approx(7.0 / LN2, rel=1e-12)      # (8-1)/ln2
    assert r.auc0inf == pytest.approx(8.0 / LN2, rel=1e-12)    # C0/k
    assert r.pct_extrap == pytest.approx(12.5, abs=1e-9)
    assert not any("extrapolation" in f for f in r.flags)      # 12.5% < 20%


def test_pct_extrap_over_20_sets_flag():
    """Barely-declined profile: %extrap = Clast/C0 = exp(-0.3) ~ 74% > 20%."""
    C0, k = 100.0, 0.1
    profile = [(t, C0 * math.exp(-k * t)) for t in (0.0, 1.0, 2.0, 3.0)]
    r = compute_nca(profile, lloq=0.01)

    assert r.pct_extrap == pytest.approx(100.0 * math.exp(-0.3), rel=1e-9)
    assert r.pct_extrap > 20.0
    assert any("extrapolation" in f for f in r.flags)


# ---------------------------------------------------------------------------
# lambda_z selection details
# ---------------------------------------------------------------------------
def test_ars_prefers_better_fitting_shorter_window():
    """The ARS rule must reject an earlier off-line point.

    Peak at t=0.5; terminal candidates at t = 1,2,3,4.  The last 3 (t=2,3,4)
    lie exactly on a slope -0.5 line (adj R^2 = 1).  Adding t=1 (off the line)
    drops adj R^2 well past the 0.0001 tolerance, so the 3-point window wins.
    """
    profile = [
        (0.0, None),
        (0.5, 30.0),          # Cmax / Tmax
        (1.0, 20.0),          # off the terminal line
        (2.0, 10.0),
        (3.0, 10.0 * math.exp(-0.5)),
        (4.0, 10.0 * math.exp(-1.0)),
    ]
    r = compute_nca(profile, lloq=0.1)
    assert r.lambda_z_n_points == 3
    assert r.lambda_z == pytest.approx(0.5, rel=1e-9)
    assert r.lambda_z_r2 == pytest.approx(1.0, abs=1e-12)


def test_lambda_z_not_estimable_with_too_few_terminal_points():
    """Only one point after Tmax -> lambda_z not estimable, tail params None,
    but AUC0-t is still returned.
    """
    r = compute_nca([(0.0, 10.0), (1.0, 5.0)], lloq=0.01)
    assert r.lambda_z is None
    assert r.thalf is None
    assert r.auc0inf is None
    assert r.pct_extrap is None
    assert "lambda_z not estimable" in r.flags
    assert r.auc0t == pytest.approx(5.0 / LN2, abs=1e-9)   # (0,10)->(1,5) log-down


# ---------------------------------------------------------------------------
# Container / batch API
# ---------------------------------------------------------------------------
def test_to_dict_roundtrip():
    r = compute_nca([(0.0, 8.0), (1.0, 4.0), (2.0, 2.0), (3.0, 1.0)], lloq=0.01)
    d = r.to_dict()
    assert isinstance(d, dict)
    assert d["cmax"] == 8.0
    assert d["auc0t_method"] == "linear-up/log-down"
    assert d["pct_extrap"] == pytest.approx(12.5, abs=1e-9)
    assert isinstance(d["flags"], list)
    # every dataclass field is present
    for key in (
        "cmax", "tmax", "auc0t", "auc0inf", "lambda_z", "thalf", "pct_extrap",
        "clast", "tlast", "auc0t_method", "lambda_z_n_points", "lambda_z_r2",
        "lambda_z_r2adj", "lambda_z_intercept", "flags",
    ):
        assert key in d


def test_compute_profiles_maps_keys_to_results():
    profiles = {
        ("subj1", "R"): [(0.0, 8.0), (1.0, 4.0), (2.0, 2.0), (3.0, 1.0)],
        ("subj1", "T"): [(0.0, 16.0), (1.0, 8.0), (2.0, 4.0), (3.0, 2.0)],
    }
    out = compute_profiles(profiles, lloq=0.01)
    assert set(out.keys()) == set(profiles.keys())
    assert all(isinstance(v, NCAResult) for v in out.values())
    assert out[("subj1", "R")].cmax == 8.0
    assert out[("subj1", "T")].cmax == 16.0
    # both halve each hour -> identical terminal slope (ln2)
    assert out[("subj1", "R")].lambda_z == pytest.approx(LN2, rel=1e-9)
    assert out[("subj1", "T")].lambda_z == pytest.approx(LN2, rel=1e-9)


def test_defensive_sorting_of_unordered_input():
    """Input need not be time-sorted."""
    ordered = compute_nca([(0.0, 8.0), (1.0, 4.0), (2.0, 2.0), (3.0, 1.0)], lloq=0.01)
    shuffled = compute_nca([(2.0, 2.0), (0.0, 8.0), (3.0, 1.0), (1.0, 4.0)], lloq=0.01)
    assert shuffled.auc0t == pytest.approx(ordered.auc0t, rel=1e-12)
    assert shuffled.lambda_z == pytest.approx(ordered.lambda_z, rel=1e-12)
    assert shuffled.cmax == ordered.cmax


def test_unsupported_blq_rule_raises():
    with pytest.raises(ValueError):
        compute_nca([(0.0, 8.0), (1.0, 4.0)], lloq=0.01, blq_rule="zero")
