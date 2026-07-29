"""Tests for the PK sampling-schedule engine.

The engine exists to stop three irreversible clinical errors, so the tests are
written to FAIL if any of them could happen:

  1. A clipped Cmax - the peak must be bracketed on BOTH sides, for every
     combination of Tmax and half-life the product range can throw at it.
  2. A blown AUC extrapolation - the tail must reach far enough that the
     predicted AUC_%extrap stays under 20%, or the engine must say loudly that
     it cannot.
  3. An unestimable lambda_z - at least three (four for a two-compartment drug)
     samples in the terminal window.

Plus the honesty invariants: a missing Tmax is DECLARED, never silently
fabricated; and the blood volume is added up against a stated healthy-volunteer
limit rather than assumed to be fine.
"""
from __future__ import annotations

import json
import math

import pytest

from services.sampling_schedule import (
    AUC_EXTRAP_LIMIT_PCT,
    MAX_BLOOD_VOLUME_8WEEK_ML,
    MIN_LAMBDA_Z_POINTS,
    MIN_LAMBDA_Z_POINTS_MULTICOMPARTMENT,
    PK_DRAW_VOLUME_ML,
    PREDOSE_CARRYOVER_LIMIT_PCT,
    TERMINAL_MIN_HALF_LIVES,
    TERMINAL_TARGET_HALF_LIVES,
    _CATALOGUE,
    compute_sampling_schedule,
    extract_reported_timepoints,
    sampling_schedule_directive,
    verify_sampling_schedule,
)

# A deliberately wide sweep: 20-minute-Tmax fast analgesic through to a
# 40-day-half-life monoclonal antibody.
PK_SWEEP = [
    # (t_half_hours, tmax_hours, design_key, study_type, is_biologic)
    (1.0, 0.5, "crossover", "BE", False),        # very fast small molecule
    (2.5, 0.33, "crossover", "BE", False),       # 20 min Tmax
    (6.0, 1.0, "crossover", "BE", False),        # textbook IR oral
    (12.0, 3.0, "crossover", "BE", False),
    (40.0, 4.0, "replicate", "BE", False),       # warfarin-like NTI
    (72.0, 6.0, "parallel", "BE", False),        # long small molecule
    (10.0, 8.0, "crossover", "BE", False),       # Tmax close to t1/2 (ER-like)
    (2.0, 12.0, "crossover", "BE", False),       # flip-flop: Tmax >> t1/2
    (13.0, 5.0, "crossover", "biosimilar_pk", True),   # filgrastim-like
    (336.0, 120.0, "parallel", "biosimilar_pk", True), # IgG1 mAb
    (480.0, 168.0, "parallel", "biosimilar_pk", True), # 20-day t1/2 mAb
]


def _times(result):
    return [tp["hours"] for tp in result["timepoints"]]


def _walk_strings(obj):
    """Every string anywhere in the result (for the ASCII guard)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


# ---------------------------------------------------------------------------
# Catalogue integrity
# ---------------------------------------------------------------------------


def test_catalogue_is_well_formed():
    keys = [m.key for m in _CATALOGUE]
    assert len(keys) == len(set(keys)), "duplicate modifier keys"
    for m in _CATALOGUE:
        assert m.label and m.effect and m.evidence and m.applies_when
        # The rationale is printed into the protocol - it must be an argument,
        # not a stub.
        assert len(m.rationale) > 80, f"{m.key} rationale too thin to justify a row"
        assert m.applies is False and m.detected_from_label is False


# ---------------------------------------------------------------------------
# Failure mode 1 - a clipped Cmax
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("t_half,tmax,design,study,biologic", PK_SWEEP)
def test_peak_is_bracketed_on_both_sides(t_half, tmax, design, study, biologic):
    """The whole engine exists to stop this: if the true Tmax sits between two
    draws, Cmax is biased low in both arms and no re-analysis recovers it."""
    r = compute_sampling_schedule(t_half, tmax, design, study, is_biologic=biologic)
    pb = r["peak_bracketing"]
    assert pb["points_before_tmax"] >= 2, "ascending limb of the peak is under-sampled"
    assert pb["points_after_tmax"] >= 2, "descending limb of the peak is under-sampled"
    # Tmax itself must be one of the nominal times.
    assert any(abs(t - pb["tmax_hours"]) < 1e-6 for t in _times(r))
    assert not any(f["key"] == "peak_under_bracketed" for f in r["flags"])


def test_a_fifteen_minute_tmax_is_still_bracketed_on_both_sides():
    """The peak cluster is laid out as FRACTIONS of Tmax, so for a very fast
    drug several of those fractions land inside one 5-minute grid slot and
    collapse into a single draw. The window the bracketing is counted over must
    therefore be snapped OUTWARD, or a schedule that genuinely brackets the rise
    (draws at 5 and 10 min for a 15-min Tmax) is scored as clipping it."""
    r = compute_sampling_schedule(2.0, 0.25, "crossover", "BE")
    pb = r["peak_bracketing"]
    assert pb["points_before_tmax"] >= 2
    assert pb["points_after_tmax"] >= 2
    assert not any(f["key"] == "peak_under_bracketed" for f in r["flags"])
    # The draws are real clock times a site can hit, not 7.5-minute fictions.
    # (Times are stored to 4 dp, so 5 min is 0.0833 h - whole minutes within a
    # couple of seconds, which is the precision a nominal time is written to.)
    early = [tp for tp in r["timepoints"] if 0 < tp["hours"] <= 0.25]
    assert len(early) >= 3
    for tp in early:
        minutes = tp["hours"] * 60.0
        assert abs(minutes - round(minutes)) < 0.05, (
            f"{tp['hours']} h is not a schedulable whole-minute draw")
        assert tp["label"].endswith("min")


@pytest.mark.parametrize("t_half", [0.5, 2.0, 6.0, 24.0, 96.0, 336.0])
@pytest.mark.parametrize("tmax", [0.25, 0.5, 1.0, 3.0, 8.0, 24.0, 120.0])
def test_peak_is_bracketed_across_the_whole_pk_grid(t_half, tmax):
    """A broad sweep, because the failure is silent: any (t1/2, Tmax) pair the
    product range can produce must either bracket the peak or SAY it cannot."""
    r = compute_sampling_schedule(t_half, tmax, "crossover", "BE")
    pb = r["peak_bracketing"]
    bracketed = pb["points_before_tmax"] >= 2 and pb["points_after_tmax"] >= 2
    assert bracketed or any(f["key"] == "peak_under_bracketed"
                            and f["severity"] == "critical" for f in r["flags"])
    assert bracketed, (
        f"t1/2={t_half} h, Tmax={tmax} h: peak not bracketed "
        f"({pb['points_before_tmax']} before, {pb['points_after_tmax']} after)")


@pytest.mark.parametrize("t_half,tmax,design,study,biologic", PK_SWEEP)
def test_predose_and_absorption_phase_precede_the_peak(t_half, tmax, design, study, biologic):
    r = compute_sampling_schedule(t_half, tmax, design, study, is_biologic=biologic)
    times = _times(r)
    assert times[0] == 0.0, "every profile starts with a pre-dose baseline draw"
    assert r["timepoints"][0]["phase"] == "pre-dose"
    # At least two post-dose draws strictly before Tmax define the rise.
    assert sum(1 for t in times if 0 < t < r["peak_bracketing"]["tmax_hours"]) >= 2


# ---------------------------------------------------------------------------
# Failure mode 2 - a blown AUC extrapolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("t_half,tmax,design,study,biologic", PK_SWEEP)
def test_tail_keeps_auc_extrapolation_under_the_regulatory_limit(
    t_half, tmax, design, study, biologic
):
    r = compute_sampling_schedule(t_half, tmax, design, study, is_biologic=biologic)
    if r["auc_extrap_within_limit"]:
        assert r["auc_extrap_pct"] <= AUC_EXTRAP_LIMIT_PCT
        assert r["last_timepoint_half_lives"] >= TERMINAL_MIN_HALF_LIVES - 0.05
    else:
        # It is allowed to be unachievable - but only if it is SHOUTED about.
        assert any(f["key"] == "auc_extrap_exceeds_limit" and f["severity"] == "critical"
                   for f in r["flags"])
        assert r["must_disclose"] is True


def test_auc_extrap_is_the_first_order_formula_not_a_guess():
    """AUC_%extrap = exp(-lambda_z * t_last); 5 half-lives -> 3.125%."""
    r = compute_sampling_schedule(6.0, 1.0, "crossover", "BE")
    lam = math.log(2.0) / 6.0
    expected = 100.0 * math.exp(-lam * r["last_timepoint_hours"])
    assert r["auc_extrap_pct"] == pytest.approx(expected, abs=0.01)
    assert r["lambda_z_per_hour"] == pytest.approx(lam, abs=1e-5)
    # 5 x t1/2 is the target horizon, so the prediction lands on 2^-5.
    assert r["last_timepoint_hours"] == pytest.approx(30.0)
    assert r["last_timepoint_half_lives"] == pytest.approx(5.0, abs=0.05)
    assert r["auc_extrap_pct"] == pytest.approx(3.125, abs=0.01)

    # Grid snapping may push the horizon LATER but never EARLIER. Pulling the
    # last draw back onto the clock grid (a 200 h target landing on Day 8 =
    # 192 h) would quietly raise the extrapolation the protocol has promised,
    # and it is the one sample that cannot be repeated.
    for t_half, tmax, study, biologic in (
        (10.0, 2.0, "BE", False),
        (40.0, 4.0, "BE", False),            # 200 h target, whole-day grid
        (13.0, 5.0, "biosimilar_pk", True),  # 65 h target, 4-hour grid
        (336.0, 120.0, "biosimilar_pk", True),
    ):
        s = compute_sampling_schedule(t_half, tmax, "parallel", study,
                                      is_biologic=biologic)
        assert s["horizon_truncated"] is False
        assert s["last_timepoint_half_lives"] >= TERMINAL_TARGET_HALF_LIVES
        # 5 half-lives leaves at most 2^-5 = 3.125% extrapolated.
        assert s["auc_extrap_pct"] <= 100.0 * 2.0 ** -TERMINAL_TARGET_HALF_LIVES


@pytest.mark.parametrize("t_half,tmax,design,study,biologic", PK_SWEEP)
def test_snapping_never_shortens_the_tail_below_the_target(
    t_half, tmax, design, study, biologic
):
    """Whatever the grid, an untruncated horizon reaches at least the target it
    was computed from - so the AUC_%extrap the protocol states is achievable."""
    r = compute_sampling_schedule(t_half, tmax, design, study, is_biologic=biologic)
    if not r["horizon_truncated"]:
        assert r["last_timepoint_hours"] >= (
            TERMINAL_TARGET_HALF_LIVES * r["t_half_hours"] - 1e-6)


def test_the_practical_ceiling_is_modality_specific_and_declared():
    """A 14-day half-life is routine follow-up for a biologic and impossible
    inside a small-molecule BE study. The engine must not paper over that: the
    same PK treated as a small molecule has to truncate AND say so."""
    bio = compute_sampling_schedule(336.0, 120.0, "parallel", "biosimilar_pk",
                                    is_biologic=True)
    assert bio["horizon_truncated"] is False
    assert bio["auc_extrap_within_limit"] is True

    sm = compute_sampling_schedule(336.0, 120.0, "parallel", "BE")
    assert sm["horizon_truncated"] is True
    assert sm["last_timepoint_hours"] < bio["last_timepoint_hours"]
    assert sm["auc_extrap_within_limit"] is False
    keys = {f["key"] for f in sm["flags"]}
    assert "horizon_truncated" in keys and "auc_extrap_exceeds_limit" in keys
    assert sm["must_disclose"] is True


def test_impractically_long_half_life_is_truncated_and_the_breach_is_declared():
    """A 40-day half-life cannot be followed for 5 half-lives inside a real
    study, so the engine must truncate AND admit the consequence rather than
    quietly promising an extrapolation it cannot deliver."""
    r = compute_sampling_schedule(960.0, 168.0, "parallel", "biosimilar_pk",
                                  is_biologic=True)
    assert r["horizon_truncated"] is True
    assert r["auc_extrap_pct"] > AUC_EXTRAP_LIMIT_PCT
    assert r["auc_extrap_within_limit"] is False
    keys = {f["key"] for f in r["flags"]}
    assert "auc_extrap_exceeds_limit" in keys
    assert "horizon_truncated" in keys
    # The remediation a reviewer needs is spelled out, not left implicit.
    msg = next(f["message"] for f in r["flags"] if f["key"] == "auc_extrap_exceeds_limit")
    assert "AUC(0-t)" in msg


# ---------------------------------------------------------------------------
# Failure mode 3 - an unestimable terminal slope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("t_half,tmax,design,study,biologic", PK_SWEEP)
def test_lambda_z_has_enough_points_in_the_terminal_window(
    t_half, tmax, design, study, biologic
):
    r = compute_sampling_schedule(t_half, tmax, design, study, is_biologic=biologic)
    assert r["lambda_z_estimable"] is True, (
        "lambda_z is unestimable - AUC(0-inf), t1/2 and CL/F could not be reported")
    assert r["terminal_points"] >= MIN_LAMBDA_Z_POINTS
    counted = [tp for tp in r["timepoints"] if tp["lambda_z_point"]]
    assert len(counted) == r["terminal_points"]


def test_two_compartment_label_demands_a_fourth_terminal_point():
    """The early post-peak points belong to the distribution phase, so a
    biexponential drug needs one more point before lambda_z can be fitted."""
    label = {"clinical_pharmacology": (
        "Plasma concentrations decline in a biexponential manner, with a rapid "
        "distribution phase followed by a slower terminal phase.")}
    r = compute_sampling_schedule(10.0, 2.0, "crossover", "BE", label=label)
    assert "multi_compartment" in {m["key"] for m in r["modifiers"]}
    assert r["min_terminal_points_required"] == MIN_LAMBDA_Z_POINTS_MULTICOMPARTMENT
    assert r["terminal_points"] >= MIN_LAMBDA_Z_POINTS_MULTICOMPARTMENT

    plain = compute_sampling_schedule(10.0, 2.0, "crossover", "BE")
    assert plain["min_terminal_points_required"] == MIN_LAMBDA_Z_POINTS


def test_elimination_sampling_is_log_spaced_not_linear():
    """Equal time steps waste draws on the early tail. Log spacing means the
    gaps grow - the last gap must be several times the first."""
    r = compute_sampling_schedule(24.0, 2.0, "crossover", "BE")
    elim = [tp["hours"] for tp in r["timepoints"] if tp["phase"] == "elimination"]
    assert len(elim) >= 5
    gaps = [b - a for a, b in zip(elim, elim[1:])]
    assert gaps[-1] > 3 * gaps[0], "elimination sampling looks linear, not log-spaced"
    assert max(gaps) > 3 * min(gaps)


# ---------------------------------------------------------------------------
# Honesty about missing inputs
# ---------------------------------------------------------------------------


def test_unknown_tmax_is_declared_and_flagged_not_fabricated():
    r = compute_sampling_schedule(12.0, None, "crossover", "BE")
    assert r["tmax_known"] is False
    assert r["tmax_basis"] == "assumed"
    assert any(a["key"] == "tmax_hours" for a in r["assumptions"])
    flag = next(f for f in r["flags"] if f["key"] == "tmax_assumed")
    assert flag["severity"] == "critical"
    assert flag["must_surface_in_protocol"] is True
    assert r["must_disclose"] is True
    assert "assumed" in r["summary"].lower()
    # It still returns a defensible schedule rather than nothing.
    assert r["n_timepoints"] >= 12
    assert r["auc_extrap_within_limit"] is True


def test_unknown_half_life_is_declared_and_flagged():
    r = compute_sampling_schedule(None, 1.0, "crossover", "BE")
    assert r["t_half_known"] is False
    assert r["t_half_basis"] == "assumed"
    assert any(f["key"] == "t_half_assumed" for f in r["flags"])
    assert any(a["key"] == "t_half_hours" for a in r["assumptions"])
    assert r["must_disclose"] is True


def test_known_inputs_raise_no_assumption_flags():
    r = compute_sampling_schedule(6.0, 1.0, "crossover", "BE")
    assert r["tmax_known"] is True and r["t_half_known"] is True
    assert r["assumptions"] == []
    assert r["flags"] == []
    assert r["must_disclose"] is False


def test_assumed_tmax_never_exceeds_the_half_life():
    """A drug that is gone in an hour cannot have its peak cluster placed at the
    generic 2 h oral default."""
    r = compute_sampling_schedule(0.75, None, "crossover", "BE")
    assert r["tmax_hours"] <= 0.75 + 1e-9
    assert r["last_timepoint_hours"] > r["tmax_hours"]


# ---------------------------------------------------------------------------
# The biologic case - days and weeks, not hours
# ---------------------------------------------------------------------------


def test_biologic_schedule_spans_weeks_and_is_expressed_in_study_days():
    r = compute_sampling_schedule(336.0, 120.0, "parallel", "biosimilar_pk",
                                  is_biologic=True)
    assert r["is_biologic"] is True
    assert r["last_timepoint_hours"] >= 30 * 24, "a 14-day t1/2 needs weeks of follow-up"
    labels = [tp["label"] for tp in r["timepoints"]]
    assert any(lab.startswith("Day ") for lab in labels)
    last = r["timepoints"][-1]
    assert last["day"] >= 30
    # Late visits get a realistic window; +/- 2 min at Day 70 is not schedulable.
    assert last["window_minutes"] >= 60
    # Early confinement draws stay tight.
    assert r["timepoints"][1]["window_minutes"] <= 60


def test_biosimilar_study_always_gets_an_ada_series_but_a_small_molecule_never_does():
    bio = compute_sampling_schedule(336.0, 120.0, "parallel", "biosimilar_pk",
                                    is_biologic=True)
    assert "immunogenicity" in {m["key"] for m in bio["modifiers"]}
    ada = bio["immunogenicity_timepoints"]
    assert len(ada) >= 4
    assert ada[0]["hours"] == 0.0
    assert bio["blood_volume"]["ada_volume_ml"] > 0

    # An oral small molecule whose label happens to mention antibodies must NOT
    # acquire an ADA assay.
    sm = compute_sampling_schedule(
        6.0, 1.0, "crossover", "BE",
        label={"clinical_pharmacology": "Anti-drug antibodies were not assessed."})
    assert sm["immunogenicity_timepoints"] == []
    assert "immunogenicity" not in {m["key"] for m in sm["modifiers"]}


# ---------------------------------------------------------------------------
# Design awareness
# ---------------------------------------------------------------------------


def test_crossover_repeats_the_profile_and_adds_the_carry_over_rule():
    xo = compute_sampling_schedule(6.0, 1.0, "crossover", "BE")
    par = compute_sampling_schedule(6.0, 1.0, "parallel", "BE")

    assert xo["periods"] == 2 and par["periods"] == 1
    assert xo["blood_volume"]["pk_volume_ml"] == pytest.approx(
        2 * par["blood_volume"]["pk_volume_ml"])

    # The 5% pre-dose carry-over rule is a multi-period concept only.
    assert xo["predose_carryover_rule"] is not None
    assert f"{PREDOSE_CARRYOVER_LIMIT_PCT:.0f}%" in xo["predose_carryover_rule"]
    assert par["predose_carryover_rule"] is None
    assert "carry-over" in xo["timepoints"][0]["purpose"]
    assert "carry-over" not in par["timepoints"][0]["purpose"]


def test_replicate_design_charges_four_periods_of_blood():
    rep = compute_sampling_schedule(6.0, 1.0, "replicate", "BE")
    xo = compute_sampling_schedule(6.0, 1.0, "crossover", "BE")
    assert rep["periods"] == 4
    assert rep["blood_volume"]["pk_volume_ml"] == pytest.approx(
        2 * xo["blood_volume"]["pk_volume_ml"])


def test_the_same_timepoints_are_used_in_every_period():
    """Periods change the volume, never the profile - a different schedule per
    period would make the within-subject comparison invalid."""
    xo = compute_sampling_schedule(6.0, 1.0, "crossover", "BE")
    par = compute_sampling_schedule(6.0, 1.0, "parallel", "BE")
    assert _times(xo) == _times(par)


# ---------------------------------------------------------------------------
# Blood volume
# ---------------------------------------------------------------------------


def test_blood_volume_is_totalled_against_a_stated_limit():
    r = compute_sampling_schedule(6.0, 1.0, "crossover", "BE")
    bv = r["blood_volume"]
    assert bv["draw_volume_ml"] == PK_DRAW_VOLUME_ML
    assert bv["limit_ml"] <= MAX_BLOOD_VOLUME_8WEEK_ML
    assert "8-week" in bv["limit_basis"]
    assert bv["pk_volume_ml"] == pytest.approx(
        bv["draws_per_period"] * bv["periods"] * PK_DRAW_VOLUME_ML)
    assert bv["total_ml"] >= bv["pk_volume_ml"]
    assert bv["within_limit"] is True
    # Cumulative volume on the last draw is the per-period total.
    assert r["timepoints"][-1]["cumulative_volume_ml"] == pytest.approx(
        r["n_timepoints"] * PK_DRAW_VOLUME_ML)


def test_excessive_blood_volume_is_flagged_with_a_usable_remedy():
    """Four periods of 6 mL draws blows the healthy-volunteer ceiling. The
    engine must say so AND give the draw volume that would comply."""
    r = compute_sampling_schedule(24.0, 2.0, "replicate", "BE", draw_volume_ml=6.0)
    bv = r["blood_volume"]
    assert bv["max_8_week_ml"] > bv["limit_ml"]
    assert bv["within_limit"] is False
    flag = next(f for f in r["flags"] if f["key"] == "blood_volume_exceeds_limit")
    assert flag["severity"] == "critical"
    assert bv["compliant_draw_volume_ml"] < 6.0
    # The remedy has to actually fit under the limit.
    fixed = compute_sampling_schedule(
        24.0, 2.0, "replicate", "BE", draw_volume_ml=bv["compliant_draw_volume_ml"])
    assert fixed["blood_volume"]["within_limit"] is True


def test_widely_separated_periods_do_not_all_land_in_one_8_week_window():
    """A long washout pushes later periods out of the 8-week window the limit is
    written against, so the worst window is less than the study total."""
    r = compute_sampling_schedule(40.0, 4.0, "replicate", "BE")
    bv = r["blood_volume"]
    assert bv["max_8_week_ml"] < bv["total_ml"]
    assert bv["within_limit"] is True


# ---------------------------------------------------------------------------
# Multiple-dose / steady state
# ---------------------------------------------------------------------------


def test_multiple_dose_uses_auc_tau_and_does_not_extrapolate():
    r = compute_sampling_schedule(8.0, 2.0, "crossover", "BE", dosing="multiple",
                                  tau_hours=12.0)
    assert r["dosing"] == "multiple"
    assert r["auc_metric"] == "AUC(0-tau)"
    assert r["auc_extrap_pct"] is None, "there is nothing to extrapolate at steady state"
    assert r["auc_extrap_within_limit"] is True
    assert r["last_timepoint_hours"] == pytest.approx(12.0)
    assert r["dosing_interval_hours"] == pytest.approx(12.0)
    assert len(r["steady_state_troughs"]) == 3
    assert not any(f["key"] == "auc_extrap_exceeds_limit" for f in r["flags"])


def test_missing_dosing_interval_is_assumed_and_flagged():
    r = compute_sampling_schedule(8.0, 2.0, "crossover", "BE", dosing="multiple")
    assert r["dosing_interval_hours"] == pytest.approx(24.0)
    assert any(f["key"] == "tau_assumed" for f in r["flags"])
    assert r["must_disclose"] is True


def test_steady_state_profile_does_not_demand_a_terminal_slope():
    """lambda_z is fitted from the post-final-dose decline, never from a profile
    that covers one dosing interval. Raising a critical flag for it here would
    be noise that trains a reviewer to ignore the flags that matter."""
    ss = compute_sampling_schedule(48.0, 3.0, "crossover", "BE", dosing="multiple",
                                   tau_hours=12.0)
    assert ss["lambda_z_required"] is False
    assert not any(f["key"] == "lambda_z_not_estimable" for f in ss["flags"])
    assert "AUC(0-tau)" in sampling_schedule_directive(ss)

    sd = compute_sampling_schedule(48.0, 3.0, "crossover", "BE")
    assert sd["lambda_z_required"] is True
    assert sd["lambda_z_estimable"] is True


def test_tmax_beyond_the_dosing_interval_is_named_as_a_regimen_problem():
    """A steady-state Tmax at or past tau means the regimen and the PK disagree.
    Reporting only 'Cmax can be clipped' points the reader at the schedule when
    it is the dosing interval that is wrong."""
    r = compute_sampling_schedule(60.0, 24.0, "crossover", "BE", dosing="multiple",
                                  tau_hours=12.0)
    flag = next(f for f in r["flags"] if f["key"] == "tmax_beyond_dosing_interval")
    assert flag["severity"] == "critical"
    assert flag["must_surface_in_protocol"] is True
    assert "tau" in flag["message"]
    assert r["must_disclose"] is True
    sampling_schedule_directive(r).encode("ascii")

    ok = compute_sampling_schedule(60.0, 3.0, "crossover", "BE", dosing="multiple",
                                   tau_hours=12.0)
    assert not any(f["key"] == "tmax_beyond_dosing_interval" for f in ok["flags"])


def test_single_dose_has_no_steady_state_troughs():
    r = compute_sampling_schedule(8.0, 2.0, "crossover", "BE")
    assert r["steady_state_troughs"] == []
    assert r["auc_metric"].startswith("AUC(0-t)")


# ---------------------------------------------------------------------------
# Label-grounded modifiers
# ---------------------------------------------------------------------------


def test_modified_release_label_widens_the_peak_window():
    """An extended-release Tmax is later and far more variable, so the cluster
    has to stretch past the immediate-release 2 x Tmax."""
    er = compute_sampling_schedule(
        10.0, 6.0, "crossover", "BE",
        label={"how_supplied": "Extended-release tablets, 100 mg"})
    ir = compute_sampling_schedule(10.0, 6.0, "crossover", "BE")
    assert "modified_release" in {m["key"] for m in er["modifiers"]}
    assert er["peak_bracketing"]["peak_window_hours"][1] > \
        ir["peak_bracketing"]["peak_window_hours"][1]
    assert er["peak_bracketing"]["points_after_tmax"] > \
        ir["peak_bracketing"]["points_after_tmax"]


def test_food_effect_study_widens_the_peak_even_without_a_label_cue():
    """One schedule has to bracket both the fasted and the fed peak; sizing it
    on the fasting Tmax manufactures a food effect out of a sampling artefact."""
    fe = compute_sampling_schedule(10.0, 6.0, "crossover", "food_effect")
    be = compute_sampling_schedule(10.0, 6.0, "crossover", "BE")
    mod = next(m for m in fe["modifiers"] if m["key"] == "food_effect")
    assert mod["detected_from_label"] is False, "applied by study design, not label"
    assert fe["peak_bracketing"]["peak_window_hours"][1] > \
        be["peak_bracketing"]["peak_window_hours"][1]


def test_enterohepatic_label_inserts_mid_phase_draws():
    label = {"pharmacokinetics": (
        "The drug undergoes enterohepatic recirculation, producing a secondary "
        "peak approximately 12 hours after dosing.")}
    r = compute_sampling_schedule(10.0, 6.0, "crossover", "BE", label=label)
    assert "secondary_peak" in {m["key"] for m in r["modifiers"]}
    times = _times(r)
    for expected in (2.5 * 6.0, 3.5 * 6.0):
        assert any(abs(t - expected) <= 1.0 for t in times), (
            f"no draw near {expected} h to catch the second peak")
    plain = compute_sampling_schedule(10.0, 6.0, "crossover", "BE")
    assert len(times) > len(_times(plain))


def test_iv_infusion_label_renames_the_peak_draw():
    label = {"dosage_and_administration": (
        "Dilute and administer as an intravenous infusion over 30 minutes.")}
    r = compute_sampling_schedule(8.0, 0.5, "parallel", "BE", label=label)
    assert "iv_infusion" in {m["key"] for m in r["modifiers"]}
    peak = next(tp for tp in r["timepoints"]
                if abs(tp["hours"] - r["peak_bracketing"]["tmax_hours"]) < 1e-6)
    assert "end-of-infusion" in peak["purpose"].lower()
    assert "contralateral" in peak["purpose"].lower()


def test_active_metabolite_label_adds_a_supportive_analyte_without_extra_draws():
    label = {"clinical_pharmacology": "It is converted to a pharmacologically active metabolite."}
    r = compute_sampling_schedule(8.0, 2.0, "crossover", "BE", label=label)
    plain = compute_sampling_schedule(8.0, 2.0, "crossover", "BE")
    assert "active_metabolite" in {m["key"] for m in r["modifiers"]}
    assert len(r["analytes"]) == 2
    assert "parent" in r["analytes"][1].lower()
    assert _times(r) == _times(plain), "the metabolite is measured in the same aliquot"


def test_unrelated_label_triggers_no_modifiers():
    label = {"pharmacokinetics": (
        "Absorption is rapid and the drug is widely distributed into tissues.")}
    r = compute_sampling_schedule(6.0, 1.0, "crossover", "BE", label=label)
    assert r["modifiers"] == []
    # The full catalogue is still reported, all marked inapplicable.
    assert len(r["considered"]) == len(_CATALOGUE)
    assert all(m["applies"] is False for m in r["considered"])


def test_no_label_does_not_crash():
    r = compute_sampling_schedule(6.0, 1.0, "crossover", "BE", label=None)
    assert r["n_timepoints"] > 10
    assert r["summary"]


# ---------------------------------------------------------------------------
# Structural integrity / serialisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("t_half,tmax,design,study,biologic", PK_SWEEP)
def test_times_are_strictly_increasing_and_indexed(t_half, tmax, design, study, biologic):
    r = compute_sampling_schedule(t_half, tmax, design, study, is_biologic=biologic)
    times = _times(r)
    assert times == sorted(times)
    assert len(set(times)) == len(times), "a subject cannot be drawn twice at one time"
    assert [tp["index"] for tp in r["timepoints"]] == list(range(1, len(times) + 1))
    cumulative = [tp["cumulative_volume_ml"] for tp in r["timepoints"]]
    assert cumulative == sorted(cumulative)
    assert times[-1] == pytest.approx(r["last_timepoint_hours"])


def test_result_is_json_serialisable_for_population_config():
    """It is persisted inside population_config (JSONB), so it must round-trip."""
    r = compute_sampling_schedule(
        336.0, 120.0, "parallel", "biosimilar_pk", is_biologic=True,
        label={"how_supplied": "Extended-release; anti-drug antibodies were measured."})
    restored = json.loads(json.dumps(r))
    assert restored["timepoints"][0]["hours"] == r["timepoints"][0]["hours"]
    assert restored["auc_extrap_pct"] == r["auc_extrap_pct"]
    assert restored["blood_volume"] == r["blood_volume"]


@pytest.mark.parametrize("t_half,tmax,design,study,biologic", PK_SWEEP)
def test_output_is_pure_ascii(t_half, tmax, design, study, biologic):
    """Windows consoles are cp1252; a stray unicode dash in a message has broken
    this repo before."""
    r = compute_sampling_schedule(t_half, tmax, design, study, is_biologic=biologic)
    for s in _walk_strings(r):
        s.encode("ascii")
    sampling_schedule_directive(r).encode("ascii")


# ---------------------------------------------------------------------------
# The prompt directive
# ---------------------------------------------------------------------------


def test_directive_locks_every_timepoint_for_the_prompt():
    r = compute_sampling_schedule(6.0, 1.0, "crossover", "BE")
    d = sampling_schedule_directive(r)
    assert "ALREADY DETERMINED" in d
    for tp in r["timepoints"]:
        assert str(tp["hours"]) in d
    assert "blood volume" in d.lower()
    assert "AUC_%extrap" in d
    assert str(PREDOSE_CARRYOVER_LIMIT_PCT).rstrip("0").rstrip(".") in d
    # Empty input yields no directive rather than a misleading empty header.
    assert sampling_schedule_directive({}) == ""
    assert sampling_schedule_directive({"timepoints": []}) == ""


def test_directive_surfaces_assumptions_and_must_state_flags():
    r = compute_sampling_schedule(None, None, "crossover", "BE")
    d = sampling_schedule_directive(r)
    assert "ASSUMPTION to disclose" in d
    assert "MUST STATE IN THE PROTOCOL" in d


# ---------------------------------------------------------------------------
# QC re-verification helper
# ---------------------------------------------------------------------------


def _as_markdown_table(schedule, times=None):
    times = _times(schedule) if times is None else times
    rows = ["| Nominal Time (h) | Acceptable Window | Volume per Draw (mL) |",
            "|---|---|---|"]
    rows += [f"| {t} | +/- 5 min | 4 |" for t in times]
    return "\n".join(rows)


def test_verifier_passes_a_faithful_schedule():
    s = compute_sampling_schedule(6.0, 1.0, "crossover", "BE")
    out = verify_sampling_schedule(_as_markdown_table(s), s)
    assert out["status"] == "pass"
    assert out["missing"] == []
    assert out["predose_present"] is True


def test_verifier_fails_a_tail_that_stops_early():
    """The model writing '...through 6 hours' for a drug that needs 30 would
    blow the AUC extrapolation - that must be a hard failure, not a nit."""
    s = compute_sampling_schedule(6.0, 1.0, "crossover", "BE")
    truncated = [t for t in _times(s) if t <= 6.0]
    out = verify_sampling_schedule(_as_markdown_table(s, truncated), s)
    assert out["status"] == "fail"
    assert "falls short" in out["detail"]
    assert out["reported_last_hours"] <= 6.0


def test_verifier_fails_a_sparse_peak():
    """One draw near Tmax clips Cmax; the verifier must catch it even though the
    tail is the right length."""
    s = compute_sampling_schedule(6.0, 1.0, "crossover", "BE")
    sparse = [0.0, 1.0, 4.0, 9.5, 20.0, 30.0]
    out = verify_sampling_schedule(_as_markdown_table(s, sparse), s)
    assert out["status"] == "fail"
    assert "clipped" in out["detail"]


def test_verifier_warns_when_it_cannot_parse_a_schedule():
    s = compute_sampling_schedule(6.0, 1.0, "crossover", "BE")
    out = verify_sampling_schedule(
        "Blood samples will be collected at appropriate intervals post-dose.", s)
    assert out["status"] == "warning"
    assert out["missing"] == _times(s)


def test_verifier_is_not_applicable_without_a_computed_schedule():
    assert verify_sampling_schedule("anything", {})["status"] == "not_applicable"
    assert verify_sampling_schedule("anything", None)["status"] == "not_applicable"


def test_extract_reported_timepoints_reads_tables_prose_and_days():
    table = "| Nominal Time (h) | x |\n|---|---|\n| Pre-dose | a |\n| 0.5 | b |\n| 24 | c |\n| Day 8 (168) | d |"
    assert extract_reported_timepoints(table) == [0.0, 0.5, 24.0, 168.0]
    assert extract_reported_timepoints("Samples at 0.5 h, 1 h, 2 h and 24 hours.") == \
        [0.5, 1.0, 2.0, 24.0]
    assert extract_reported_timepoints("") == []
