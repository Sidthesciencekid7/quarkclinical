"""The consent form, the CRF visit schedule and the blood-volume audit state the
protocol's ENGINE-LOCKED washout - they no longer recompute 7 x t1/2 themselves.

``routers/protocols.py::_lock_washout`` decides the washout between periods
(engine default 7 x t1/2, or an investigator-specified override) and writes it
into ``population_config["_sample_size"]``. The protocol narrative already used
that value; ``services/icf.py::_washout``, ``services/crf_dmp.py::_washout_days``
and ``services/sampling_schedule.py::_blood_volume_audit`` each re-derived
7 x t1/2, so a requested 14-day washout would have left the consent form and the
visit schedule disagreeing with the protocol.

Contract pinned here:

* locked value present  -> that exact value (override or default);
* locked "not applicable" (parallel) -> no washout, as before;
* locked "applicable, no figure" (no half-life) -> no invented figure;
* keys ABSENT (a protocol stored before the lock) -> the original 7 x t1/2
  computation, bit-for-bit. The "before" expectations below are written out
  independently of the code under test.

Pure tests: no database, no network, no model.
"""
from __future__ import annotations

import math
import os

import pytest

os.environ.setdefault("MOCK_AI", "1")

from routers.protocols import _lock_washout
from services import icf as icf_mod
from services.ai_service import _locked_design_directive
from services.biostatistics import WASHOUT_HALF_LIVES, format_washout_duration
from services.crf_dmp import _washout_days, build_crf_spec
from services.icf import build_icf
from services.sampling_schedule import compute_sampling_schedule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ss(design_key: str = "crossover", t_half=24.0, **extra) -> dict:
    """A sample-size dict of the shape the router stores, BEFORE the lock."""
    ss = {
        "design": ("parallel-group" if design_key == "parallel"
                   else "2-period, 2-sequence crossover"),
        "design_key": design_key,
        "approach": "ABE", "endpoint": "Cmax, AUC0-inf",
        "cv_pct": 25.0, "cv_source": "literature", "gmr": 0.95, "power": 0.9,
        "alpha": 0.05, "lower_limit": 0.80, "upper_limit": 1.25,
        "n_enrolled": 28, "n_evaluable": 24, "dropout_rate": 0.15,
        "t_half_hours": t_half,
    }
    ss.update(extra)
    return ss


def _locked(design_key: str = "crossover", t_half=24.0, override=None) -> dict:
    """The sample-size block AFTER the real _lock_washout (default or override)."""
    ss = _ss(design_key, t_half)
    cfg = {} if override is None else {"washout_days": override}
    _lock_washout(ss, cfg)
    return ss


def _icf_washout(ss: dict, *, pk_t_half=None) -> dict:
    ctx = {"drug_inn": "metformin", "study_type": "BE", "sample_size": ss}
    if pk_t_half is not None:
        ctx["pk_parameters"] = [{"parameter": "t_half", "value": pk_t_half}]
    return build_icf(ctx)["study_facts"]["washout"]


def _icf(ss: dict) -> dict:
    return build_icf({"drug_inn": "metformin", "study_type": "BE", "sample_size": ss})


def _section_text(icf: dict, key: str) -> str:
    s = next(s for s in icf["sections"] if s["key"] == key)
    return " ".join(list(s["body"]) + list(s["bullets"]))


def _legacy_icf_washout(t_half: float) -> dict:
    """The consent form's washout BEFORE the lock existed, written out
    independently of services/icf.py (7 x t1/2; whole hours rounded up under a
    day, otherwise days to one decimal)."""
    hours = WASHOUT_HALF_LIVES * t_half
    if hours < 24.0:
        text = f">= {math.ceil(hours)} hours"
        plain = (f"There is a gap of at least {math.ceil(hours)} hours between "
                 "stays so the medicine leaves your body.")
    else:
        text = f">= {hours / 24.0:.1f} days"
        plain = (f"There is a gap of about {hours / 24.0:.0f} days between stays "
                 "so the medicine leaves your body.")
    return {
        "applicable": True,
        "hours": round(hours, 1),
        "days": round(hours / 24.0, 1),
        "protocol_text": text,
        "plain": plain,
        "basis": f"{WASHOUT_HALF_LIVES:.0f} x terminal half-life (t1/2 = {t_half:.1f} h)",
    }


_T_HALVES = [2.0, 2.1, 3.4, 6.2, 12.0, 24.0, 40.0, 168.0, 336.0]


# ---------------------------------------------------------------------------
# ICF - a user override
# ---------------------------------------------------------------------------


def test_icf_states_the_overridden_washout_not_seven_half_lives():
    ss = _locked("crossover", 24.0, override=14)   # engine default would be 7 days
    w = _icf_washout(ss)
    assert w["applicable"] is True
    assert w["days"] == 14.0
    assert w["hours"] == 336.0
    assert w["protocol_text"] == "14 days"
    assert "14 days" in w["plain"]
    assert w["locked"] is True and w["is_user_override"] is True
    assert "user override" in w["basis"]


def test_icf_override_text_is_the_protocols_text():
    """The consent form and the protocol's locked-design block name the same
    figure in the same words."""
    ss = _locked("crossover", 24.0, override=14)
    assert _icf_washout(ss)["protocol_text"] == format_washout_duration(14)
    assert "Washout between EVERY period: 14 days" in _locked_design_directive(ss)


def test_icf_duration_section_uses_the_override():
    ss = _locked("crossover", 24.0, override=14)
    icf = _icf(ss)
    assert "Gap between stays: 14 days." in _section_text(icf, "duration_and_visits")
    duration = icf["study_facts"]["duration"]
    assert duration["washout_days_between_periods"] == 14.0
    # 2 periods -> exactly one washout in the study length.
    expected_total = math.ceil(
        duration["screening_window_days"]
        + duration["periods"] * duration["days_per_period"]
        + 14.0
        + duration["follow_up_days"]
    )
    assert duration["total_days"] == expected_total


def test_icf_override_with_a_fraction_is_not_rounded_to_whole_days():
    ss = _locked("crossover", 24.0, override=10.5)
    icf = _icf(ss)
    assert icf["study_facts"]["washout"]["protocol_text"] == "10.5 days"
    text = _section_text(icf, "duration_and_visits")
    assert "Gap between stays: 10.5 days." in text
    assert "about 10 days" not in text


def test_icf_does_not_recompute_from_the_half_life_when_the_washout_is_locked():
    """A different t1/2 reaching the ICF (e.g. from pk_parameters) must not
    change a locked washout."""
    ss = _locked("crossover", 24.0, override=14)
    assert icf_mod._washout(ss, 99.0)["days"] == 14.0
    # And end to end: PK records disagreeing with the locked t1/2 change nothing.
    assert _icf_washout(ss, pk_t_half=500.0)["days"] == 14.0


# ---------------------------------------------------------------------------
# ICF - the locked engine default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("t_half", [6.2, 12.0, 24.0, 40.0, 336.0])
def test_icf_locked_default_equals_the_engine_default_and_the_old_wording(t_half):
    ss = _locked("crossover", t_half)
    assert ss["washout_is_user_override"] is False
    w = _icf_washout(ss)
    legacy = _legacy_icf_washout(t_half)
    # Same figure, same wording as before for a >= 1 day default.
    for key in ("applicable", "days", "protocol_text", "plain", "basis"):
        assert w[key] == legacy[key], key
    assert w["hours"] == pytest.approx(ss["washout_hours"], abs=0.05)
    assert w["locked"] is True and w["is_user_override"] is False


def test_icf_locked_default_is_the_figure_the_protocol_directive_states():
    ss = _locked("crossover", 40.0)    # 7 x 40 h = 11.7 days
    assert _icf_washout(ss)["protocol_text"] == ">= 11.7 days"
    assert "Washout between EVERY period: >= 11.7 days" in _locked_design_directive(ss)


def test_icf_locked_default_under_a_day_is_stated_in_whole_hours():
    ss = _locked("crossover", 2.1)     # 14.7 h -> 15 h
    w = _icf_washout(ss)
    assert w["protocol_text"] == ">= 15 hours"
    assert "at least 15 hours" in w["plain"]
    assert ">= 15 hours" in _locked_design_directive(ss)


# ---------------------------------------------------------------------------
# ICF - parallel and "no figure"
# ---------------------------------------------------------------------------


def test_icf_locked_parallel_is_unchanged_from_a_legacy_parallel():
    locked = _icf_washout(_locked("parallel", 600.0))
    legacy = _icf_washout(_ss("parallel", 600.0))       # no lock keys at all
    assert locked == legacy
    assert locked["applicable"] is False
    assert locked["hours"] is None and locked["days"] is None
    assert "no gap" in locked["plain"].lower()
    icf = _icf(_locked("parallel", 600.0))
    assert icf["study_facts"]["periods"] == 1
    assert "gap between stays" not in _section_text(icf, "duration_and_visits").lower()


def test_icf_locked_without_a_figure_gets_the_placeholder_not_an_invented_number():
    ss = _locked("crossover", None)          # lock: applicable, no half-life
    assert ss["washout_applicable"] is True and ss["washout_days"] is None
    expected = icf_mod._washout(_ss("crossover", None), None)  # the legacy placeholder
    # Even if the ICF can find a t1/2 elsewhere, the protocol states none.
    for w in (icf_mod._washout(ss, None), icf_mod._washout(ss, 24.0)):
        assert w == expected
        assert w["days"] is None and w["hours"] is None
        assert w["protocol_text"] == icf_mod._PLACEHOLDER
    assert any(p["field"] == "washout" for p in _icf(ss)["pending_fields"])
    assert _icf_washout(ss, pk_t_half=24.0)["days"] is None


# ---------------------------------------------------------------------------
# ICF - protocols stored before the lock: unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("t_half", _T_HALVES)
def test_icf_without_lock_keys_is_identical_to_the_original_computation(t_half):
    ss = _ss("crossover", t_half)
    assert "washout_applicable" not in ss and "washout_days" not in ss
    assert _icf_washout(ss) == _legacy_icf_washout(t_half)
    # Exactly the original keys - nothing added for an older protocol.
    assert set(_icf_washout(ss)) == {
        "applicable", "hours", "days", "protocol_text", "plain", "basis"}


def test_icf_without_lock_keys_and_without_a_half_life_keeps_the_placeholder():
    w = _icf_washout(_ss("crossover", None))
    assert w["applicable"] is True and w["days"] is None
    assert w["protocol_text"] == icf_mod._PLACEHOLDER
    assert "no half-life available" in w["basis"]


def test_icf_without_lock_keys_still_takes_the_half_life_from_pk_records():
    w = _icf_washout(_ss("crossover", None), pk_t_half=12.0)
    assert w == _legacy_icf_washout(12.0)


def test_icf_duration_bullet_for_an_older_protocol_keeps_its_wording():
    icf = _icf(_ss("crossover", 40.0))            # 11.7 days
    assert "Gap between stays: about 12 days." in _section_text(icf, "duration_and_visits")


# ---------------------------------------------------------------------------
# CRF / DMP - _washout_days
# ---------------------------------------------------------------------------


def test_crf_washout_is_the_override():
    ss = _locked("crossover", 12.0, override=14)    # default would be 3.5
    assert _washout_days(12.0, "crossover", ss) == 14.0
    # The locked value wins over an explicitly passed half-life.
    assert _washout_days(999.0, "crossover", ss) == 14.0


def test_crf_washout_is_the_engine_default_when_the_default_is_locked():
    ss = _locked("crossover", 12.0)
    assert ss["washout_is_user_override"] is False
    assert _washout_days(12.0, "crossover", ss) == pytest.approx(3.5)
    ss40 = _locked("crossover", 40.0)
    assert _washout_days(40.0, "crossover", ss40) == pytest.approx(11.7)


def test_crf_locked_parallel_and_parallel_design_have_no_washout():
    assert _washout_days(600.0, "parallel", _locked("parallel", 600.0)) is None
    # A parallel design has none whatever the block says.
    assert _washout_days(12.0, "parallel", _locked("crossover", 12.0, override=14)) is None
    assert _washout_days(12.0, "parallel") is None


def test_crf_locked_not_applicable_states_no_washout():
    assert _washout_days(12.0, "crossover", {"washout_applicable": False}) is None


def test_crf_locked_without_a_figure_states_none_even_if_a_half_life_is_passed():
    ss = _locked("crossover", None)
    assert ss["washout_applicable"] is True and ss["washout_days"] is None
    assert _washout_days(12.0, "crossover", ss) is None


@pytest.mark.parametrize("t_half", _T_HALVES)
@pytest.mark.parametrize("design", ["crossover", "replicate"])
def test_crf_without_lock_keys_is_the_original_seven_half_lives(t_half, design):
    expected = round(WASHOUT_HALF_LIVES * t_half / 24.0, 1)
    assert _washout_days(t_half, design) == expected
    assert _washout_days(t_half, design, {}) == expected
    assert _washout_days(t_half, design, _ss(design, t_half)) == expected


def test_crf_without_lock_keys_and_without_a_half_life_is_none():
    for t_half in (None, 0, -3):
        assert _washout_days(t_half, "crossover") is None
        assert _washout_days(t_half, "crossover", _ss("crossover", None)) is None


# ---------------------------------------------------------------------------
# CRF / DMP - build_crf_spec end to end
# ---------------------------------------------------------------------------


def _washout_visits(spec: dict) -> list[dict]:
    return [v for v in spec["visit_schedule"] if v["epoch"] == "WASHOUT"]


def _assumption(spec: dict, key: str) -> dict:
    return next(a for a in spec["assumptions"] if a["key"] == key)


def test_crf_spec_carries_the_override_into_visits_and_assumptions():
    ss = _locked("crossover", 12.0, override=14)
    spec = build_crf_spec({"sample_size": ss})
    assert spec["design_key"] == "crossover" and spec["periods"] == 2
    assert spec["washout_days"] == 14.0
    visits = _washout_visits(spec)
    assert len(visits) == 1 and visits[0]["timing"] == "14 days"
    assumption = _assumption(spec, "washout_days")
    assert assumption["value"] == 14.0
    assert "Investigator-specified" in assumption["basis"]
    assert "7 x terminal t1/2" not in assumption["basis"]
    assert "washout_unknown" not in {f["key"] for f in spec["flags"]}


def test_crf_spec_with_the_locked_default_matches_the_original_arithmetic():
    ss = _locked("crossover", 12.0)
    spec = build_crf_spec({"sample_size": ss})
    assert spec["washout_days"] == pytest.approx(3.5)
    assert _washout_visits(spec)[0]["timing"] == "3.5 days"
    assert _assumption(spec, "washout_days")["basis"].startswith("7 x terminal t1/2")


def test_crf_spec_without_lock_keys_is_unchanged():
    spec = build_crf_spec(design_key="crossover", t_half_hours=12.0)
    assert spec["washout_days"] == pytest.approx(3.5)
    assert _washout_visits(spec)[0]["timing"] == "3.5 days"
    assert _assumption(spec, "washout_days")["basis"].startswith("7 x terminal t1/2")
    # The same through an older stored sample-size block.
    spec_old = build_crf_spec({"sample_size": _ss("crossover", 12.0)})
    assert spec_old["washout_days"] == spec["washout_days"]


def test_crf_spec_parallel_has_no_washout_visit():
    spec = build_crf_spec({"sample_size": _locked("parallel", 600.0)})
    assert spec["periods"] == 1
    assert spec["washout_days"] is None
    assert _washout_visits(spec) == []


def test_crf_spec_flags_a_locked_washout_without_a_figure_rather_than_inventing_one():
    """The sampling schedule can carry an ASSUMED t1/2; the CRF must not turn it
    into a washout the protocol does not state."""
    ss = _locked("crossover", None)
    sampling = {"timepoints": [], "periods": 2, "t_half_hours": 12.0,
                "design_key": "crossover", "is_biologic": False}
    spec = build_crf_spec({"sample_size": ss}, sampling=sampling)
    assert spec["periods"] == 2
    assert spec["washout_days"] is None
    assert "washout_unknown" in {f["key"] for f in spec["flags"]}
    assert all(v["timing"] == "Protocol-specified washout" for v in _washout_visits(spec))


# ---------------------------------------------------------------------------
# Sampling schedule - blood-volume audit
# ---------------------------------------------------------------------------


def _audit(washout_days=None, design="crossover") -> dict:
    kwargs = {} if washout_days is None else {"washout_days": washout_days}
    return compute_sampling_schedule(24.0, 2.0, design, "BE", **kwargs)["blood_volume"]


def test_blood_volume_audit_default_is_unchanged_without_a_locked_washout():
    assert _audit() == _audit(None)
    # The locked default equals 7 x t1/2 exactly for t1/2 = 24 h: no difference.
    assert _audit(7.0) == _audit()


def test_blood_volume_audit_spaces_periods_by_the_locked_washout():
    default, long_gap = _audit(), _audit(60.0)
    # A 60-day washout pushes period 2 out of any single 8-week window.
    assert long_gap["max_8_week_ml"] < default["max_8_week_ml"]
    assert long_gap["total_ml"] == default["total_ml"]     # total blood is unchanged
    # A modest override that keeps both periods in one window changes nothing.
    assert _audit(14.0) == default


def test_blood_volume_audit_ignores_a_locked_washout_for_parallel():
    assert _audit(60.0, "parallel") == _audit(None, "parallel")


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "60", [60]])
def test_blood_volume_audit_falls_back_to_the_default_for_an_unusable_value(bad):
    # inf would put every period infinitely far apart; it is not a real washout.
    assert _audit(bad) == _audit()


# ---------------------------------------------------------------------------
# Through the real engine pipeline (_augment_config_with_design, no I/O)
# ---------------------------------------------------------------------------


def _pk_records(t_half: float) -> list:
    from types import SimpleNamespace

    from models import PKParameter

    return [SimpleNamespace(
        parameter=PKParameter.t_half, value=t_half, unit="h",
        population="healthy volunteers", source_pmid="1", confidence=1.0,
    )]


def _augment(population_config: dict, t_half: float = 24.0) -> dict:
    from routers.protocols import _augment_config_with_design

    return _augment_config_with_design(
        population_config=population_config,
        study_type="BE",
        drug_inn="metoprolol",
        extracted=[],
        pk_params=_pk_records(t_half),
        label=None,
    )


@pytest.fixture(scope="module")
def pipeline_override_cfg() -> dict:
    return _augment({"washout_days": 14})


@pytest.fixture(scope="module")
def pipeline_default_cfg() -> dict:
    return _augment({})


def test_pipeline_override_reaches_the_protocol_the_icf_and_the_crf(pipeline_override_cfg):
    cfg = pipeline_override_cfg
    ss = cfg["_sample_size"]
    assert ss["washout_days"] == 14.0 and ss["washout_is_user_override"] is True
    # The protocol narrative ...
    assert "Washout between EVERY period: 14 days" in _locked_design_directive(ss)
    # ... the consent form ...
    washout = cfg["_icf"]["study_facts"]["washout"]
    assert washout["days"] == 14.0 and washout["protocol_text"] == "14 days"
    # ... and the CRF visit schedule all state the one locked figure.
    assert cfg["_crf_dmp"]["washout_days"] == 14.0
    assert [v["timing"] for v in cfg["_crf_dmp"]["visit_schedule"]
            if v["epoch"] == "WASHOUT"] == ["14 days"]


def test_pipeline_default_washout_agrees_across_protocol_icf_and_crf(pipeline_default_cfg):
    cfg = pipeline_default_cfg
    ss = cfg["_sample_size"]
    assert ss["washout_is_user_override"] is False
    assert ss["washout_days"] == pytest.approx(7.0)     # 7 x 24 h
    assert cfg["_icf"]["study_facts"]["washout"]["days"] == pytest.approx(7.0)
    assert cfg["_crf_dmp"]["washout_days"] == pytest.approx(7.0)
