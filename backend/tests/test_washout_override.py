"""Washout between periods is an ENGINE input, not something the model derives.

Before this, the locked-design prompt block restated "7 x t1/2" and the model
wrote the number, so (a) a requested washout ("make it 14 days") had nowhere to
go, (b) nothing validated it, and (c) the synopsis and study design could state
different figures. The washout is now decided once, with its source, in
``routers/protocols.py::_lock_washout`` (modelled on the CV override), validated
by ``biostatistics.validate_washout_override``, narrated verbatim via
``ai_service._locked_design_directive`` and checked in prose by
``protocol_qc._check_locked_washout``.

Pure tests: no database, no network, no model.
"""
from __future__ import annotations

import math
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("MOCK_AI", "1")

from models import PKParameter
from routers.protocols import _augment_config_with_design, _lock_washout
from services.ai_service import (
    _SECTION_CONFIGS,
    _build_section_prompt,
    _locked_design_directive,
)
from services.biostatistics import (
    MAX_WASHOUT_OVERRIDE_DAYS,
    WASHOUT_HALF_LIVES,
    default_washout_days,
    engine_default_washout,
    format_washout_duration,
    validate_washout_override,
)
from services.protocol_qc import (
    QCStatus,
    _find_washout_hours,
    _washout_mentions,
    run_qc,
)


# A drug with t1/2 = 24 h: 7 x t1/2 = 168 h = exactly 7 days, so every boundary
# below is an exact, readable number.
T_HALF_24 = 24.0


def _pk(t_half: float) -> list:
    """Minimal PK records (the shape _median_t_half reads) for one t1/2."""
    return [SimpleNamespace(
        parameter=PKParameter.t_half, value=t_half, unit="h",
        population="healthy volunteers", source_pmid="1", confidence=1.0,
    )]


def _ss(design_key: str = "crossover", t_half=T_HALF_24, **extra) -> dict:
    """A sample-size dict of the shape the router stores (pre-lock)."""
    ss = {
        "cv_pct": 25.0, "cv_source": "literature", "gmr": 0.95, "power": 0.9,
        "alpha": 0.05, "lower_limit": 0.80, "upper_limit": 1.25,
        "design": "2-period crossover, single-dose", "design_key": design_key,
        "approach": "ABE", "n_per_group": None, "n_enrolled": 28,
        "n_evaluable": 24, "dropout_rate": 0.15, "endpoint": "Cmax, AUC0-inf",
        "needs_simulation": False, "t_half_hours": t_half,
    }
    ss.update(extra)
    return ss


# ---------------------------------------------------------------------------
# 1. validate_washout_override
# ---------------------------------------------------------------------------


def test_crossover_14_days_at_or_above_7_half_lives_is_accepted():
    ok, minimum, message = validate_washout_override("crossover", T_HALF_24, 14)
    assert ok is True
    assert minimum == pytest.approx(7.0)
    assert "14 days" in message and "7 days" in message


def test_replicate_design_also_carries_a_washout():
    assert validate_washout_override("replicate", T_HALF_24, 10)[0] is True


def test_exactly_the_minimum_is_accepted():
    ok, minimum, _ = validate_washout_override("crossover", T_HALF_24, 7)
    assert ok is True and minimum == pytest.approx(7.0)


def test_parallel_design_is_rejected_because_it_has_no_washout():
    ok, minimum, message = validate_washout_override("parallel", T_HALF_24, 14)
    assert ok is False
    assert minimum is None
    assert "parallel design has no washout period" in message


def test_parallel_is_rejected_even_when_the_half_life_is_unknown():
    ok, _, message = validate_washout_override("parallel", None, 14)
    assert ok is False and "no washout period" in message


def test_too_short_is_rejected_and_the_minimum_is_stated_in_days():
    ok, minimum, message = validate_washout_override("crossover", T_HALF_24, 6)
    assert ok is False
    assert minimum == pytest.approx(7.0)
    assert "7 days" in message
    assert "6 days" in message
    assert "7 x t1/2" in message


def test_minimum_follows_the_half_life():
    # t1/2 = 40 h -> 280 h = 11.7 days (the engine's one-decimal statement).
    ok, minimum, message = validate_washout_override("crossover", 40.0, 11)
    assert ok is False and minimum == pytest.approx(11.7)
    assert "11.7 days" in message
    assert validate_washout_override("crossover", 40.0, 11.7)[0] is True


def test_unknown_half_life_is_rejected_conservatively():
    """The floor cannot be checked, so the override is refused (not accepted
    with a flag): a washout under 7 x t1/2 risks carryover."""
    for t_half in (None, 0, -5, float("nan")):
        ok, minimum, message = validate_washout_override("crossover", t_half, 14)
        assert ok is False
        assert minimum is None
        assert "half-life" in message


@pytest.mark.parametrize("bad", [0, -3, -0.5, float("nan"), float("inf"), "abc", None, True, [14]])
def test_non_positive_or_non_numeric_values_are_rejected(bad):
    ok, _, message = validate_washout_override("crossover", T_HALF_24, bad)
    assert ok is False
    assert "positive number of days" in message


def test_absurd_values_are_capped():
    assert MAX_WASHOUT_OVERRIDE_DAYS == 365.0
    assert validate_washout_override("crossover", T_HALF_24, 365)[0] is True
    ok, _, message = validate_washout_override("crossover", T_HALF_24, 366)
    assert ok is False and "365" in message


def test_unknown_design_is_rejected():
    ok, _, message = validate_washout_override(None, T_HALF_24, 14)
    assert ok is False and "crossover or replicate" in message


# ---------------------------------------------------------------------------
# 2. The engine default
# ---------------------------------------------------------------------------


def test_default_washout_is_seven_half_lives_and_reuses_the_constant():
    assert WASHOUT_HALF_LIVES == 7.0
    d = engine_default_washout(40.0)
    assert d["exact_hours"] == pytest.approx(WASHOUT_HALF_LIVES * 40.0)
    assert d["days"] == pytest.approx(11.7)
    assert d["hours"] == pytest.approx(280.8)
    assert default_washout_days(24.0) == pytest.approx(7.0)


def test_default_washout_under_a_day_is_stated_in_whole_hours_rounded_up():
    d = engine_default_washout(2.1)  # 14.7 h
    assert d["hours"] == 15.0
    assert d["days"] == pytest.approx(15 / 24)
    assert format_washout_duration(d["days"]) == "15 hours"


def test_default_washout_is_none_without_a_half_life():
    for t_half in (None, 0, -1, "x", float("nan")):
        assert engine_default_washout(t_half) is None
        assert default_washout_days(t_half) is None


def test_format_washout_duration():
    assert format_washout_duration(14) == "14 days"
    assert format_washout_duration(1) == "1 day"
    assert format_washout_duration(11.7) == "11.7 days"
    assert format_washout_duration(10.25) == "10.25 days"
    assert format_washout_duration(0.5) == "12 hours"


# ---------------------------------------------------------------------------
# 3. _lock_washout: the number and its source are set together
# ---------------------------------------------------------------------------


def test_default_washout_is_computed_and_locked_when_no_override_is_given():
    ss, cfg = _ss("crossover", 40.0), {}
    _lock_washout(ss, cfg)
    assert ss["washout_applicable"] is True
    assert ss["washout_days"] == pytest.approx(11.7)
    assert ss["washout_hours"] == pytest.approx(280.8)
    assert ss["washout_is_user_override"] is False
    assert ss["washout_source"].startswith("engine: 7 x t1/2 = 11.7 days")
    assert "washout_days" not in cfg  # nothing is written into the input config


def test_override_is_locked_with_its_source():
    ss, cfg = _ss("crossover", T_HALF_24), {"washout_days": 14}
    _lock_washout(ss, cfg)
    assert ss["washout_days"] == 14.0
    assert ss["washout_hours"] == 336.0
    assert ss["washout_is_user_override"] is True
    assert ss["washout_minimum_days"] == pytest.approx(7.0)
    assert ss["washout_source"].startswith("user override")
    assert cfg["washout_days"] == 14.0


def test_numeric_string_override_is_coerced_and_blank_is_ignored():
    ss, cfg = _ss(), {"washout_days": " 10 "}
    _lock_washout(ss, cfg)
    assert ss["washout_days"] == 10.0 and cfg["washout_days"] == 10.0

    ss, cfg = _ss(), {"washout_days": "  "}
    _lock_washout(ss, cfg)
    assert ss["washout_is_user_override"] is False
    assert "washout_days" not in cfg


@pytest.mark.parametrize(
    "design_key, t_half, value, fragment",
    [
        ("parallel", T_HALF_24, 14, "parallel design has no washout period"),
        ("crossover", T_HALF_24, 3, "minimum of 7 days"),
        ("crossover", None, 14, "half-life"),
        ("crossover", T_HALF_24, "fourteen", "positive number of days"),
        ("crossover", T_HALF_24, 4000, "365"),
    ],
)
def test_invalid_override_raises_422_with_the_reason(design_key, t_half, value, fragment):
    with pytest.raises(HTTPException) as err:
        _lock_washout(_ss(design_key, t_half), {"washout_days": value})
    assert err.value.status_code == 422
    assert fragment in err.value.detail


def test_parallel_without_an_override_is_locked_as_not_applicable():
    ss = _ss("parallel", 600.0)
    _lock_washout(ss, {})
    assert ss["washout_applicable"] is False
    assert ss["washout_days"] is None and ss["washout_hours"] is None
    assert "parallel" in ss["washout_source"]


def test_crossover_with_unknown_half_life_gets_no_invented_number():
    ss = _ss("crossover", None)
    _lock_washout(ss, {})
    assert ss["washout_applicable"] is True
    assert ss["washout_days"] is None
    assert "no terminal half-life" in ss["washout_source"]


# ---------------------------------------------------------------------------
# 4. Through _augment_config_with_design (the real engines, no I/O)
# ---------------------------------------------------------------------------


def _augment(population_config: dict) -> dict:
    return _augment_config_with_design(
        population_config=population_config,
        study_type="BE",
        drug_inn="metoprolol",
        extracted=[],
        pk_params=_pk(T_HALF_24),
        label=None,
    )


@pytest.fixture(scope="module")
def default_cfg() -> dict:
    return _augment({})


@pytest.fixture(scope="module")
def override_cfg() -> dict:
    return _augment({"washout_days": 14})


def test_engine_default_washout_is_locked_into_sample_size(default_cfg):
    ss = default_cfg["_sample_size"]
    assert ss["design_key"] == "crossover"
    assert ss["washout_days"] == pytest.approx(7.0)
    assert ss["washout_is_user_override"] is False
    assert ss["washout_source"].startswith("engine: 7 x t1/2")


def test_override_is_locked_into_sample_size_and_the_directive(override_cfg):
    ss = override_cfg["_sample_size"]
    assert ss["washout_days"] == 14.0
    assert ss["washout_is_user_override"] is True
    assert ss["washout_source"].startswith("user override")

    directive = _locked_design_directive(ss)
    assert "Washout between EVERY period: 14 days" in directive
    assert "user override" in directive
    assert "do NOT recompute it from the half-life" in directive
    # Scope guard kept: this is not the concomitant-medication washout.
    assert "NOT the washout of a prohibited concomitant medication" in directive


def test_default_washout_reaches_the_directive_as_a_locked_number(default_cfg):
    directive = _locked_design_directive(default_cfg["_sample_size"])
    assert "Washout between EVERY period: >= 7.0 days" in directive
    assert "source: engine: 7 x t1/2" in directive
    assert "use this EXACT figure" in directive


def test_parallel_override_is_rejected_through_the_real_engines():
    with pytest.raises(HTTPException) as err:
        _augment({"design": "parallel", "washout_days": 14})
    assert err.value.status_code == 422
    assert "parallel design has no washout period" in err.value.detail


def test_too_short_override_is_rejected_through_the_real_engines():
    with pytest.raises(HTTPException) as err:
        _augment({"washout_days": 3})
    assert err.value.status_code == 422
    assert "7 days" in err.value.detail


def test_locked_washout_is_in_the_section_prompt_before_the_context(override_cfg):
    ss = override_cfg["_sample_size"]
    context = {
        "study_type": "BE", "guideline_version": "FDA-BE-2014",
        "sample_size": ss, "pk_parameters": [],
    }
    _, prompt = _build_section_prompt("study_design", _SECTION_CONFIGS["study_design"], context)
    assert "Washout between EVERY period: 14 days" in prompt
    assert prompt.index("Washout between EVERY period") < prompt.index("Context:\n")


# ---------------------------------------------------------------------------
# 5. The directive for protocols stored before the lock existed is unchanged
# ---------------------------------------------------------------------------


def test_directive_without_a_locked_washout_keeps_the_original_wording():
    directive = _locked_design_directive(_ss("crossover", 40.0))
    assert "Washout between EVERY period: >= 11.7 days (7 x t1/2, t1/2=40.0h) — use this EXACT" in directive


def test_directive_for_a_parallel_design_states_no_washout():
    directive = _locked_design_directive(_ss("parallel", 600.0, washout_days=None))
    assert "Washout: NOT APPLICABLE" in directive


def test_directive_omits_the_washout_line_when_nothing_is_known():
    assert "Washout" not in _locked_design_directive(_ss("crossover", None))


# ---------------------------------------------------------------------------
# 6. protocol_qc: the prose must state the locked washout
# ---------------------------------------------------------------------------


def _qc(ss: dict, **sections: str):
    return run_qc(
        study_type="BE",
        population_config={"_sample_size": ss},
        sections=sections,
        expected_sections=list(sections),
    )


def _locked(requested: dict | None = None) -> dict:
    """A crossover sample-size block after _lock_washout (default or override)."""
    ss = _ss("crossover", T_HALF_24)
    _lock_washout(ss, dict(requested or {}))
    return ss


def _washout_findings(report):
    return [f for f in report.findings if f.id.startswith("washout_locked")]


def test_qc_passes_when_the_prose_matches_the_locked_washout():
    ss = _locked({"washout_days": 14})
    report = _qc(
        ss,
        synopsis="<p>Subjects return after a washout of 14 days between periods.</p>",
        study_design="<p>A 14-day washout separates each dosing period.</p>",
    )
    findings = _washout_findings(report)
    assert [f.id for f in findings] == ["washout_locked"]
    assert findings[0].status is QCStatus.PASS


def test_qc_fails_when_the_prose_states_a_different_washout():
    ss = _locked({"washout_days": 14})
    report = _qc(
        ss,
        synopsis="<p>Subjects return after a washout of 14 days between periods.</p>",
        study_design="<p>Periods are separated by a washout of 10 days.</p>",
    )
    findings = _washout_findings(report)
    assert [f.id for f in findings] == ["washout_locked_study_design"]
    fail = findings[0]
    assert fail.status is QCStatus.FAIL
    assert fail.section == "study_design"
    assert "10 days" in fail.detail and "14 days" in fail.detail
    assert report.submission_ready is False


def test_qc_fails_on_a_default_washout_mismatch_too():
    ss = _locked()  # engine default: 7 days
    report = _qc(ss, study_design="<p>There is a washout of 12 days between periods.</p>")
    assert _washout_findings(report)[0].status is QCStatus.FAIL


def test_qc_warns_when_the_locked_washout_is_not_stated_at_all():
    ss = _locked({"washout_days": 14})
    report = _qc(ss, study_design="<p>Subjects are dosed in two periods.</p>")
    f = _washout_findings(report)[0]
    assert f.id == "washout_locked" and f.status is QCStatus.WARNING


def test_qc_does_not_hold_a_concomitant_medication_washout_against_the_lock():
    ss = _locked({"washout_days": 14})
    report = _qc(
        ss,
        synopsis="<p>A washout of 14 days between periods.</p>",
        population=(
            "<p>Prohibited concomitant medications must be stopped with a washout "
            "of 30 days before the first dose.</p>"
        ),
    )
    findings = _washout_findings(report)
    assert [f.id for f in findings] == ["washout_locked"]
    assert findings[0].status is QCStatus.PASS


def test_qc_does_not_hold_an_eligibility_washout_against_the_lock():
    ss = _locked({"washout_days": 14})
    report = _qc(
        ss,
        synopsis="<p>A washout of 14 days between periods.</p>",
        population="<p>Exclusion: a washout of 28 days from previous biologic therapy is required.</p>",
    )
    assert [f.status for f in _washout_findings(report)] == [QCStatus.PASS]


def test_qc_accepts_the_minimum_being_quoted_next_to_an_override():
    ss = _locked({"washout_days": 14})
    report = _qc(
        ss,
        study_design=(
            "<p>A washout of 14 days separates each period. This exceeds the "
            "minimum washout of 7 days (7 x t1/2).</p>"
        ),
    )
    assert _washout_findings(report)[0].status is QCStatus.PASS


def test_qc_check_is_silent_for_protocols_without_a_locked_washout():
    ss = _ss("crossover", T_HALF_24)  # an older stored protocol: no washout_* keys
    report = _qc(ss, study_design="<p>A washout of 12 days between periods.</p>")
    assert _washout_findings(report) == []


def test_qc_check_is_silent_for_a_parallel_design():
    ss = _ss("parallel", 600.0, washout_days=None)
    report = _qc(ss, study_design="<p>No washout applies.</p>")
    assert _washout_findings(report) == []


def test_qc_matches_weeks_and_html_markup():
    ss = _locked({"washout_days": 14})
    report = _qc(
        ss,
        study_design="<p>The <strong>washout</strong> is <em>2 weeks</em> between periods.</p>"
                     "<p>A 2-week washout is applied identically between every period.</p>",
    )
    assert _washout_findings(report)[0].status is QCStatus.PASS


# ---------------------------------------------------------------------------
# 7. The prose parser (shared with the pre-existing washout-adequacy check)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, hours",
    [
        ("a washout of at least 7 days", 168),
        ("a 7-day washout", 168),
        ("washout period of 2 weeks", 336),
        ("a washout of 36 hours", 36),
        ("a washout of 11.7 days", 280.8),
        ("an 11.7-day washout", 280.8),
        ("washout (>= 14 days)", 336),
        ("Washout: 12 days", 288),
    ],
)
def test_washout_parser_reads_decimals_and_common_phrasings(text, hours):
    assert _find_washout_hours(text) == pytest.approx(hours)


def test_washout_parser_does_not_read_half_lives_as_hours():
    """'7 half-lives' used to parse as seven HOURS (the unit 'h' matched the
    start of 'half'), which made a correct protocol look carryover-prone."""
    assert _find_washout_hours("a washout of at least 7 half-lives") is None
    assert _washout_mentions("<p>The washout is 7 half-lives long.</p>") == []


def test_washout_mentions_classifies_the_two_kinds_of_washout():
    mentions = _washout_mentions(
        "<p>A washout of 10 days separates each dosing period.</p>"
        "<p>Prohibited medications require a washout of 30 days before the first dose.</p>"
    )
    assert [(m["kind"], m["hours"]) for m in mentions] == [("between", 240.0), ("comed", 720.0)]


def test_existing_adequacy_check_no_longer_misreads_a_decimal_washout():
    """'11.7-day washout' used to parse as 7 days, tripping 'Washout too short'."""
    ss = _ss("crossover", 40.0)
    report = _qc(ss, treatment="<p>An 11.7-day washout is applied between periods.</p>")
    adequacy = [f for f in report.findings if f.id == "washout"]
    assert adequacy and adequacy[0].status is QCStatus.PASS
    assert math.isclose(40.0 * 7, 280.0)
