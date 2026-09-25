"""TMDD-aware design decisions: products with documented target-mediated drug
disposition are studied in patients on the labelled multiple-dose regimen at
steady state, and the reviewer's Population / Dose choice propagates.

Pure tests — no database, no fixtures.
"""
from __future__ import annotations

import copy

import pytest

from services import design_decisions as dd
from services.design_decisions import (
    DOSE_MULTIPLE_SS,
    DOSE_OPTIONS,
    DOSE_REDUCED,
    DOSE_SINGLE,
    POP_HEALTHY,
    POP_PATIENTS,
    POP_PATIENTS_MULTI,
    POPULATION_OPTIONS,
    DrugProfile,
    apply_decision_overrides,
    apply_engine_population_defaults,
    decisions_to_dicts,
    design_directive,
    determine_design_decisions,
    drug_tmdd,
    is_multi_dose_decision,
    reconcile_decision_set,
)


def _by_key(decisions):
    return {d.key: d for d in decisions}


def _decide(inn, study="biosimilar_pk", **kw):
    return _by_key(determine_design_decisions(drug_inn=inn, study_type=study, **kw))


@pytest.mark.parametrize(
    "inn,study",
    [(k, "biosimilar_pk") for k in dd._KB] + [("an unlisted biologic", "biosimilar_pk"), ("xyzzy", "BE")],
)
def test_options_are_uniform_and_include_multidose(inn, study):
    d = _decide(inn, study)
    assert d["population"].options == POPULATION_OPTIONS
    assert POP_PATIENTS_MULTI in d["population"].options
    assert d["dose"].options == DOSE_OPTIONS
    assert DOSE_MULTIPLE_SS in d["dose"].options


def test_kb_tmdd_values_are_valid_and_cited():
    for inn, prof in dd._KB.items():
        assert prof.tmdd in dd.TMDD_VALUES, inn
        assert prof.tmdd_note, f"{inn} has no tmdd_note"
    assert "denosumab" not in dd._KB
    assert drug_tmdd("xyzzy") == ("unknown", "")


def test_obinutuzumab_is_multidose_steady_state():
    d = _decide("obinutuzumab", design_key="parallel")
    assert d["population"].value == POP_PATIENTS_MULTI
    assert "target-mediated drug disposition (TMDD)" in d["population"].rationale
    assert "steady state" in d["population"].rationale
    assert "rather than after a single dose" in d["population"].rationale
    assert d["dose"].value == DOSE_MULTIPLE_SS
    assert d["pd_endpoint"].value.startswith("Secondary (not powered)")


def test_synthetic_tmdd_profile_without_kb():
    prof = DrugProfile("mab", "moderate", True, False, None, ("IV",), tmdd="yes", tmdd_note="x")
    assert dd._population_decision(prof, "patient").value == POP_PATIENTS_MULTI
    assert dd._population_decision(prof, "healthy").value == POP_HEALTHY


def test_trastuzumab_default_subject_unchanged_and_patient_tmdd():
    d = _decide("trastuzumab")
    assert d["population"].value == POP_HEALTHY
    assert d["dose"].value == DOSE_SINGLE
    p = _decide("trastuzumab", subject_type="patient")
    if drug_tmdd("trastuzumab")[0] == "yes":
        assert p["population"].value == POP_PATIENTS_MULTI
    else:
        assert p["population"].value == POP_PATIENTS


def test_non_tmdd_patient_request_stays_single_dose():
    assert drug_tmdd("adalimumab")[0] != "yes"
    d = _decide("adalimumab", subject_type="patient")
    assert d["population"].value == POP_PATIENTS
    assert d["dose"].value == DOSE_SINGLE


def test_doxorubicin_unchanged():
    d = _decide("doxorubicin", "BE")
    assert d["population"].value == POP_PATIENTS
    assert d["dose"].value == DOSE_REDUCED


def test_pembrolizumab_multidose_full_regimen():
    d = _decide("pembrolizumab")
    assert d["population"].value.startswith("Patients · multi-dose")
    assert d["dose"].value == DOSE_MULTIPLE_SS


def test_population_choice_overrides_engine():
    d = _decide("rituximab", population_choice=POP_PATIENTS)
    assert d["population"].value == POP_PATIENTS
    assert d["dose"].value == DOSE_SINGLE
    assert not d["pd_endpoint"].value.startswith("Secondary")
    assert "Reviewer-selected" in d["population"].rationale
    a = _decide("adalimumab", population_choice=POP_PATIENTS_MULTI)
    assert a["dose"].value == DOSE_MULTIPLE_SS
    assert a["pd_endpoint"].value.startswith("Secondary (not powered)")
    bad = _decide("adalimumab", population_choice="Aliens")
    assert bad["population"].value == POP_HEALTHY


def test_apply_decision_overrides_sets_dosing():
    def run(pop, dose=None):
        cfg = {}
        ds = [{"key": "population", "value": pop}]
        if dose:
            ds.append({"key": "dose", "value": dose})
        apply_decision_overrides(cfg, ds)
        return cfg

    c = run(POP_PATIENTS_MULTI)
    assert c["subject_type"] == "patient" and c["dosing"] == "multiple"
    assert run(POP_PATIENTS, DOSE_SINGLE)["dosing"] == "single"
    c = run(POP_HEALTHY)
    assert c["subject_type"] == "healthy" and c["dosing"] == "single"
    assert run(POP_PATIENTS, DOSE_MULTIPLE_SS)["dosing"] == "multiple"
    cfg = {}
    apply_decision_overrides(cfg, [{"key": "pd_endpoint", "value": "None (PK only)"}])
    assert "dosing" not in cfg


def test_apply_engine_population_defaults():
    ds = [{"key": "population", "value": POP_PATIENTS_MULTI}, {"key": "dose", "value": DOSE_MULTIPLE_SS}]
    cfg = {"subject_type": "healthy"}
    apply_engine_population_defaults(cfg, ds)
    assert cfg == {"subject_type": "patient", "dosing": "multiple"}
    cfg = {"subject_type": "patient", "dosing": "single"}
    apply_engine_population_defaults(cfg, ds)
    assert cfg == {"subject_type": "patient", "dosing": "single"}
    cfg = {"subject_type": "patient"}
    apply_engine_population_defaults(cfg, [{"key": "population", "value": POP_HEALTHY}])
    assert cfg["subject_type"] == "patient"


def test_reconcile_decision_set():
    md = {"key": "population", "value": POP_PATIENTS_MULTI, "rationale": "r"}
    single = {"key": "dose", "value": DOSE_SINGLE, "rationale": "r"}
    out = reconcile_decision_set([md, single], drug_inn="rituximab", study_type="biosimilar_pk")
    assert out[1]["value"] == DOSE_MULTIPLE_SS
    assert "Aligned with the multi-dose population decision" in out[1]["rationale"]

    out = reconcile_decision_set(
        [{"key": "population", "value": POP_PATIENTS, "rationale": ""},
         {"key": "dose", "value": DOSE_MULTIPLE_SS, "rationale": ""}],
        drug_inn="rituximab", study_type="biosimilar_pk")
    assert out[0]["value"] == POP_PATIENTS_MULTI

    anc = {"key": "pd_endpoint", "value": "Absolute neutrophil count (ANC) (serial CBC)", "rationale": ""}
    decisions = [md, {"key": "dose", "value": DOSE_MULTIPLE_SS, "rationale": ""}, anc]
    before = copy.deepcopy(decisions)
    out = reconcile_decision_set(decisions, drug_inn="filgrastim", study_type="biosimilar_pk")
    assert out[2]["value"].startswith("Secondary (not powered)")
    assert decisions == before

    pd_only = [anc]
    assert reconcile_decision_set(pd_only, drug_inn="filgrastim", study_type="biosimilar_pk") == pd_only

    # Reverse: single-dose healthy with a stale Secondary PD goes back to co-primary.
    stale = [{"key": "population", "value": POP_HEALTHY, "rationale": ""},
             {"key": "dose", "value": DOSE_SINGLE, "rationale": ""},
             {"key": "pd_endpoint", "value": "Secondary (not powered): Absolute neutrophil count (ANC) (serial CBC)", "rationale": ""}]
    out = reconcile_decision_set(stale, drug_inn="filgrastim", study_type="biosimilar_pk")
    assert not out[2]["value"].startswith("Secondary")
    assert reconcile_decision_set([{"key": "population", "value": None}], drug_inn="x", study_type="BE")


def test_is_multi_dose_decision():
    assert is_multi_dose_decision(POP_PATIENTS_MULTI, None)
    assert is_multi_dose_decision(None, DOSE_MULTIPLE_SS)
    assert not is_multi_dose_decision(POP_PATIENTS, DOSE_SINGLE)
    assert not is_multi_dose_decision(None, None)


def test_directive_mentions_steady_state():
    ds = determine_design_decisions(drug_inn="obinutuzumab", study_type="biosimilar_pk")
    assert "steady state" in design_directive(decisions_to_dicts(ds))
