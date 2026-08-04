"""Tests for the pre-specified safety gating engine.

The engine emits numbers a monitor acts on at 3 a.m. and a regulator reads
afterwards, so these tests pin the things whose regression would be a real
clinical error rather than a cosmetic one:

  * an individual stopping rule must never, by itself, halt the study, and a
    cohort halting rule must never read as a single-subject withdrawal;
  * a single Grade 4/5 event, related SAE, or Hy's Law case must halt dosing at
    ANY sample size - a count-scaled rule that tolerates several
    life-threatening events in a large study is not a safety rule;
  * the cumulative counts must actually move with the planned N, and stay inside
    their floor/cap;
  * Hy's Law and the ICH E14 QT boundaries must carry their real numbers;
  * a parenteral biosimilar study must gate anaphylaxis and injection sites; an
    oral BE study must not inherit rules that make no sense for it;
  * discharge must require documented physician authorisation.
"""
from __future__ import annotations

import json

import pytest

from services.safety_gating import (
    _CORE_COHORT,
    _CORE_INDIVIDUAL,
    _CTCAE_GRADES,
    _ESCALATION_RULES,
    _HALT_RELATED_G3_CAP,
    _HALT_RELATED_G3_FLOOR,
    _INJECTABLE_RULES,
    _ORAL_RULES,
    _RISK_RULES,
    _render,
    recommend_safety_gating,
    safety_gating_directive,
)

ALL_RULES = (
    _CORE_INDIVIDUAL
    + _CORE_COHORT
    + _INJECTABLE_RULES
    + _ORAL_RULES
    + _ESCALATION_RULES
    + tuple(r for rules in _RISK_RULES.values() for r in rules)
)

ORAL_LABEL = {
    "dosage_and_administration": "The recommended dose is one 500 mg tablet taken orally.",
    "pharmacokinetics": "Absorption is rapid and the drug is widely distributed.",
}

BIOLOGIC_LABEL = {
    "dosage_and_administration": (
        "Administer 40 mg by subcutaneous injection using the single-dose prefilled syringe."
    ),
    "how_supplied": "Supplied as a 40 mg/0.8 mL prefilled syringe.",
}

# Label wording that fires each risk-topic cue, so the rules that only switch on
# for an uncommon hazard are exercised as hard as the core ones. A rule that is
# only reachable through a real DailyMed label is exactly the rule that would
# otherwise ship untested and fail on the day it matters.
RISK_TOPIC_CUE_TEXT = {
    "hepatotoxicity": "Severe hepatotoxicity, including fatal hepatic failure, has occurred.",
    "qt_prolongation": "QT prolongation and torsades de pointes have been reported.",
    "bleeding": "Serious and fatal haemorrhage, including bleeding events, has occurred.",
    "hypersensitivity": "Anaphylaxis and angioedema have been reported.",
    "hypoglycemia": "Hypoglycemia is the most common adverse reaction.",
    "myelosuppression": "Severe neutropenia and thrombocytopenia have been observed.",
    "respiratory_depression": "Life-threatening respiratory depression may occur.",
    "nephrotoxicity": "Nephrotoxicity and acute kidney injury have been reported.",
    "lactic_acidosis": "Lactic acidosis has occurred in postmarketing cases.",
    "seizure": "Seizure and convulsions have been reported.",
}


# ---------------------------------------------------------------------------
# Catalogue integrity
# ---------------------------------------------------------------------------


def test_rule_catalogue_is_well_formed():
    keys = [r.key for r in ALL_RULES]
    assert len(keys) == len(set(keys)), "duplicate rule keys"
    for r in ALL_RULES:
        assert r.scope in ("individual", "cohort"), f"{r.key} has an invalid scope"
        assert r.tier in (1, 2, 3), f"{r.key} has an out-of-range tier"
        assert r.label and r.trigger and r.action and r.evidence and r.applies_when
        # A rule a pharmacologist must defend needs a real reason, not a stub.
        assert len(r.rationale) > 80, f"{r.key} rationale too thin to defend the boundary"
        # Placeholders are filled at render time; a stray brace would ship
        # literal "{k_g3}" text into a protocol.
        assert "{" not in r.rationale and "{" not in r.evidence


def test_every_catalogue_rule_renders_without_a_placeholder_crash():
    """Every rule, including the ones only a rare label cue switches on.

    label/trigger/action/applies_when go through str.format() at render time, so
    a stray literal brace or a misspelt placeholder anywhere in the catalogue is
    a latent crash that surfaces only when a real drug label happens to trip
    that rule. Rendering the whole catalogue here makes it fail at commit time
    instead.
    """
    subs = {
        "k_g3": 2, "k_qt": 2, "k_cluster": 3, "k_withdraw": 3,
        "n": 24, "n_phrase": "among the 24 planned to be dosed",
        "redose_clause": " and is not dosed in any subsequent treatment period",
    }
    for r in ALL_RULES:
        rendered = _render(r, subs)
        json.dumps(rendered)  # must survive the JSONB round trip too
        for f in ("label", "trigger", "action", "applies_when"):
            assert "{" not in rendered[f] and "}" not in rendered[f], (
                f"{r.key}: unrendered placeholder left in '{f}'")
        if r.count_key:
            assert rendered["thresholds"]["subject_count"] == subs[r.count_key], (
                f"{r.key}: count_key '{r.count_key}' did not reach the thresholds")


def test_ctcae_grade_scale_is_structured_and_complete():
    assert [g.grade for g in _CTCAE_GRADES] == [1, 2, 3, 4, 5]
    for g in _CTCAE_GRADES:
        assert g.title and g.definition and g.protocol_action
        assert len(g.definition) > 30, f"grade {g.grade} definition is not a definition"
    assert "death" in _CTCAE_GRADES[4].definition.lower()
    assert "life-threatening" in _CTCAE_GRADES[3].definition.lower()


def test_severity_and_seriousness_are_explicitly_distinguished():
    out = recommend_safety_gating(study_type="BE", n_enrolled=24)
    statement = out["grading_scale"]["severity_vs_seriousness"].lower()
    assert "intensity" in statement
    assert "congenital anomaly" in statement, "seriousness criteria must be enumerated"
    assert "independent" in statement


# ---------------------------------------------------------------------------
# The distinction that makes this correct: individual vs cohort
# ---------------------------------------------------------------------------


def test_individual_and_cohort_rules_are_disjoint_and_correctly_scoped():
    out = recommend_safety_gating(study_type="BE", design_key="crossover", n_enrolled=24)
    ind_keys = {r["key"] for r in out["individual_rules"]}
    coh_keys = {r["key"] for r in out["cohort_rules"]}
    assert ind_keys and coh_keys
    assert not (ind_keys & coh_keys), "a rule cannot be both a withdrawal and a halt"
    assert all(r["scope"] == "individual" for r in out["individual_rules"])
    assert all(r["scope"] == "cohort" for r in out["cohort_rules"])


def test_individual_rules_never_halt_the_study_themselves():
    """The classic protocol error. An individual rule may CROSS-REFERENCE a
    halting rule, but its own action must be about this subject only."""
    out = recommend_safety_gating(
        study_type="biosimilar_pk", design_key="parallel", n_enrolled=60,
        label={**BIOLOGIC_LABEL,
               "adverse_reactions": "Hepatotoxicity, seizures and lactic acidosis reported."},
    )
    for r in out["individual_rules"]:
        action = r["action"].lower()
        assert "halt dosing" not in action, (
            f"{r['key']}: an individual stopping rule must not halt the study")
        assert "is halted" not in action, f"{r['key']}: same"


@pytest.mark.parametrize("topic", sorted(RISK_TOPIC_CUE_TEXT))
def test_scope_discipline_holds_for_every_label_risk_topic(topic):
    """The individual/cohort separation must survive EVERY label-driven rule.

    The core rules are easy to keep straight; the hazard-specific ones added
    late (seizure, lactic acidosis, myelosuppression, ...) are where a
    withdrawal quietly acquires 'halt dosing of all further subjects' and turns
    one subject's event into an unintended study-wide stop.
    """
    out = recommend_safety_gating(
        study_type="BE", design_key="crossover", n_enrolled=24,
        label={**ORAL_LABEL, "warnings_and_precautions": RISK_TOPIC_CUE_TEXT[topic]},
    )
    assert topic in out["label_findings"]["risk_topics"], (
        f"the label cue for {topic} no longer fires")

    ind_keys = {r["key"] for r in out["individual_rules"]}
    coh_keys = {r["key"] for r in out["cohort_rules"]}
    assert not (ind_keys & coh_keys)
    for r in out["individual_rules"]:
        action = r["action"].lower()
        assert "halt dosing" not in action, (
            f"{r['key']}: an individual stopping rule must not halt the study")
        assert "is halted" not in action, f"{r['key']}: same"
    for r in out["cohort_rules"]:
        assert "halt" in r["action"].lower(), (
            f"{r['key']}: a cohort rule must state that further dosing stops")
    assert "{" not in safety_gating_directive(out)


def test_every_cohort_rule_actually_halts_further_dosing():
    out = recommend_safety_gating(study_type="BE", n_enrolled=24)
    for r in out["cohort_rules"]:
        assert "halt" in r["action"].lower(), (
            f"{r['key']}: a cohort rule must state that further dosing stops")


# ---------------------------------------------------------------------------
# Count scaling - a rule tuned for n=12 is wrong for n=60
# ---------------------------------------------------------------------------


def test_cumulative_halting_count_scales_with_planned_n():
    small = recommend_safety_gating(study_type="BE", n_enrolled=12)["counts"]
    large = recommend_safety_gating(study_type="BE", n_enrolled=60)["counts"]
    assert large["related_grade3_halt"] > small["related_grade3_halt"], (
        "a fixed count would be either trigger-happy at n=12 or blind at n=60")
    assert large["same_event_grade2_cluster_halt"] > small["same_event_grade2_cluster_halt"]


@pytest.mark.parametrize("n", [4, 6, 12, 24, 36, 60, 120, 400])
def test_halting_count_stays_inside_its_floor_and_cap(n):
    counts = recommend_safety_gating(study_type="BE", n_enrolled=n)["counts"]
    k = counts["related_grade3_halt"]
    assert k >= _HALT_RELATED_G3_FLOOR, (
        "a single idiosyncratic event must not halt a study")
    assert k <= _HALT_RELATED_G3_CAP, (
        "a large study must not arithmetically tolerate an absurd number of severe events")


def test_halting_count_is_monotonic_in_n():
    seen = [recommend_safety_gating(study_type="BE", n_enrolled=n)["counts"][
        "related_grade3_halt"] for n in (6, 12, 18, 24, 36, 48, 60)]
    assert seen == sorted(seen), "the boundary must never get stricter as N grows"


def test_missing_n_falls_back_to_the_conservative_floor():
    out = recommend_safety_gating(study_type="BE")
    assert out["n_dosed_planned"] is None
    assert out["counts"]["related_grade3_halt"] == _HALT_RELATED_G3_FLOOR
    assert "not available" in out["denominator_basis"]
    # The rule text must not read as if a number were known.
    rule = next(r for r in out["cohort_rules"] if r["key"] == "coh_related_grade3_count")
    assert "dosed to date" in rule["trigger"]


def test_ae_withdrawal_halt_tracks_the_design_dropout_allowance():
    """Safety boundary and powering assumption must not contradict each other."""
    out = recommend_safety_gating(study_type="BE", n_enrolled=40, dropout_rate=0.10)
    assert out["counts"]["withdrawal_fraction_used"] == 0.10
    assert out["counts"]["ae_withdrawal_halt"] == 4


# ---------------------------------------------------------------------------
# The rules that must NEVER be scaled by N
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [6, 12, 60, 600])
def test_single_grade4_or_5_event_halts_at_any_sample_size(n):
    out = recommend_safety_gating(study_type="BE", n_enrolled=n)
    rule = next(r for r in out["cohort_rules"] if r["key"] == "coh_grade4_or_5")
    trigger = rule["trigger"].lower()
    assert "any single" in trigger
    assert "not scaled" in trigger
    # No count leaked into the text or the thresholds.
    assert str(n) not in rule["trigger"]
    assert rule["thresholds"].get("subject_count") in (None, 1)


def test_single_related_sae_and_single_hys_law_case_halt_immediately():
    out = recommend_safety_gating(study_type="BE", n_enrolled=60)
    for key in ("coh_related_sae", "coh_hys_law"):
        rule = next(r for r in out["cohort_rules"] if r["key"] == key)
        assert "any single" in rule["trigger"].lower()
        assert rule["tier"] == 1


# ---------------------------------------------------------------------------
# Hepatotoxicity - Hy's Law with real numbers
# ---------------------------------------------------------------------------


def test_hys_law_carries_its_actual_numeric_definition():
    out = recommend_safety_gating(study_type="BE", n_enrolled=24)
    th = out["thresholds"]["hys_law"]
    assert th["alt_or_ast_uln_multiple"] == 3.0
    assert th["total_bilirubin_uln_multiple"] == 2.0
    assert th["alkaline_phosphatase_max_uln_multiple"] == 2.0
    assert th["alternative_cause_excluded_required"] is True

    rule = next(r for r in out["individual_rules"] if r["key"] == "ind_hys_law")
    trigger = rule["trigger"].lower()
    for token in ("alt", "ast", "3 x the upper limit of normal",
                  "total bilirubin", "2 x the upper limit of normal",
                  "alkaline phosphatase", "no alternative explanation"):
        assert token in trigger, f"Hy's Law trigger is missing '{token}'"


def test_hepatic_discontinuation_thresholds_are_present_and_numeric():
    out = recommend_safety_gating(study_type="BE", n_enrolled=24)
    th = out["thresholds"]["hepatic_discontinuation"]
    assert th["alt_ast_uln_multiple"] == 8.0
    assert th["alt_ast_persistent_uln_multiple"] == 5.0
    assert th["persistence_days"] == 14


# ---------------------------------------------------------------------------
# QT - ICH E14 boundaries
# ---------------------------------------------------------------------------


def test_qt_boundaries_use_the_ich_e14_numbers():
    out = recommend_safety_gating(study_type="BE", n_enrolled=24)
    th = out["thresholds"]["qt"]
    assert th["qtcf_absolute_ms"] == 500.0
    assert th["qtcf_delta_from_baseline_ms"] == 60.0
    rule = next(r for r in out["individual_rules"] if r["key"] == "ind_qt_prolongation")
    assert "500 ms" in rule["trigger"]
    assert "60 ms" in rule["trigger"]
    assert "baseline" in rule["trigger"].lower(), "the delta rule needs a baseline"
    assert "triplicate" in rule["trigger"].lower(), "a single automated read is not enough"


def test_qt_cohort_count_tightens_for_a_label_reporting_torsades():
    plain = recommend_safety_gating(study_type="BE", n_enrolled=24, label=ORAL_LABEL)
    risky = recommend_safety_gating(
        study_type="BE", n_enrolled=24,
        label={**ORAL_LABEL,
               "warnings_and_precautions": "Cases of QT prolongation and torsades de "
                                           "pointes have been reported."},
    )
    assert plain["counts"]["qt_boundary_subjects_halt"] == 2
    assert risky["counts"]["qt_boundary_subjects_halt"] == 1
    assert "ind_qt_predose_gate" in {r["key"] for r in risky["individual_rules"]}
    assert "ind_qt_predose_gate" not in {r["key"] for r in plain["individual_rules"]}


# ---------------------------------------------------------------------------
# Study-type / route awareness
# ---------------------------------------------------------------------------


def test_biosimilar_study_gates_anaphylaxis_and_injection_sites():
    out = recommend_safety_gating(
        study_type="biosimilar_pk", design_key="parallel", n_enrolled=60,
        label=BIOLOGIC_LABEL,
    )
    assert out["route"] == "parenteral"
    keys = {r["key"] for r in out["individual_rules"]} | {
        r["key"] for r in out["cohort_rules"]}
    assert "ind_anaphylaxis" in keys
    assert "coh_anaphylaxis" in keys
    assert "ind_injection_site_reaction" in keys
    assert "coh_injection_site_cluster" in keys


def test_oral_be_study_does_not_inherit_parenteral_gating():
    out = recommend_safety_gating(
        study_type="BE", design_key="crossover", n_enrolled=24, label=ORAL_LABEL)
    assert out["route"] == "oral"
    keys = {r["key"] for r in out["individual_rules"]} | {
        r["key"] for r in out["cohort_rules"]}
    assert "ind_anaphylaxis" not in keys
    assert "ind_injection_site_reaction" not in keys
    # ... and it DOES get the gate that only makes sense for an oral product.
    assert "ind_emesis" in keys
    emesis = next(r for r in out["individual_rules"] if r["key"] == "ind_emesis")
    assert "2 times the median tmax" in emesis["trigger"].lower()
    assert "data-handling gate, not a safety withdrawal" in emesis["action"].lower()


def test_explicit_route_overrides_the_label_inference():
    out = recommend_safety_gating(
        study_type="BE", n_enrolled=24, label=ORAL_LABEL, route="SC")
    assert out["route"] == "parenteral"
    assert "ind_anaphylaxis" in {r["key"] for r in out["individual_rules"]}


# ---------------------------------------------------------------------------
# Design awareness
# ---------------------------------------------------------------------------


def test_crossover_withdrawal_means_no_subsequent_period_dose():
    out = recommend_safety_gating(study_type="BE", design_key="crossover", n_enrolled=24)
    rule = next(r for r in out["individual_rules"] if r["key"] == "ind_related_grade3")
    assert "subsequent treatment period" in rule["action"].lower()


def test_parallel_withdrawal_does_not_promise_to_withhold_a_dose_that_never_comes():
    """In a parallel study there IS no later dose; claiming one will be withheld
    is meaningless protocol text."""
    out = recommend_safety_gating(study_type="biosimilar_pk", design_key="parallel", n_enrolled=60)
    rule = next(r for r in out["individual_rules"] if r["key"] == "ind_related_grade3")
    action = rule["action"].lower()
    assert "subsequent treatment period" not in action
    assert "single administration" in action


def test_replicate_design_is_treated_as_multi_period():
    out = recommend_safety_gating(study_type="BE", design_key="replicate", n_enrolled=30)
    rule = next(r for r in out["individual_rules"] if r["key"] == "ind_related_grade3")
    assert "subsequent treatment period" in rule["action"].lower()
    assert "multi-period" in out["discharge_criteria"]["design_note"].lower()


# ---------------------------------------------------------------------------
# Escalations - NTI, boxed warning, patient population
# ---------------------------------------------------------------------------


def test_nti_tightens_the_cohort_tally_to_a_single_subject_and_adds_sentinel_dosing():
    plain = recommend_safety_gating(study_type="BE", n_enrolled=36)
    nti = recommend_safety_gating(study_type="BE", n_enrolled=36, is_nti=True)
    assert plain["counts"]["related_grade3_halt"] > 1
    assert nti["counts"]["related_grade3_halt"] == 1
    assert "coh_sentinel_dosing" in {r["key"] for r in nti["cohort_rules"]}
    assert any(e["key"] == "narrow_therapeutic_index" for e in nti["escalations"])


@pytest.mark.parametrize("n", [12, 24, 60, 400])
def test_an_escalation_never_loosens_a_halting_boundary(n):
    """Escalations exist to tighten. A refactor that let NTI status or a patient
    population RAISE the count a study tolerates would silently make a
    higher-risk study safer on paper than its plain counterpart."""
    plain = recommend_safety_gating(study_type="BE", n_enrolled=n)["counts"]
    nti = recommend_safety_gating(study_type="BE", n_enrolled=n, is_nti=True)["counts"]
    assert nti["related_grade3_halt"] <= plain["related_grade3_halt"]
    assert nti["qt_boundary_subjects_halt"] <= plain["qt_boundary_subjects_halt"]

    # The patient uplift is the one deliberate loosening, and it is bounded to
    # exactly one event - never a proportional relaxation.
    pts = recommend_safety_gating(
        study_type="BE", n_enrolled=n, healthy_volunteers=False)["counts"]
    assert pts["related_grade3_halt"] - plain["related_grade3_halt"] == 1

    # NTI must win over the patient uplift, not be undone by it.
    nti_pts = recommend_safety_gating(
        study_type="BE", n_enrolled=n, is_nti=True, healthy_volunteers=False)["counts"]
    assert nti_pts["related_grade3_halt"] == 1


def test_boxed_warning_is_detected_from_the_label_text_and_escalates():
    label = {
        "title": "WARNING: RISK OF SERIOUS INFECTIONS - see full prescribing information "
                 "for complete boxed warning.",
        "dosage_and_administration": "One tablet orally once daily.",
    }
    out = recommend_safety_gating(study_type="BE", n_enrolled=24, label=label)
    findings = out["label_findings"]
    assert findings["boxed_warning_detected"] is True
    assert findings["boxed_warning_evidence"]
    assert findings["boxed_warning_section"] == "title", "provenance must be reported"
    assert "coh_sentinel_dosing" in {r["key"] for r in out["cohort_rules"]}
    assert any(e["key"] == "boxed_warning" for e in out["escalations"])


def test_ordinary_warnings_heading_does_not_masquerade_as_a_boxed_warning():
    """If every label escalated, the escalation would mean nothing."""
    label = {
        "warnings_and_precautions": "WARNINGS AND PRECAUTIONS: Use caution in renal impairment.",
        "dosage_and_administration": "One tablet orally once daily.",
    }
    out = recommend_safety_gating(study_type="BE", n_enrolled=24, label=label)
    assert out["label_findings"]["boxed_warning_detected"] is False
    assert "coh_sentinel_dosing" not in {r["key"] for r in out["cohort_rules"]}


def test_absent_boxed_warning_is_reported_honestly_not_as_proof():
    out = recommend_safety_gating(study_type="BE", n_enrolled=24, label=ORAL_LABEL)
    note = out["label_findings"]["note"].lower()
    assert "not found in the retrieved text" in note
    assert "no boxed" in note


def test_patient_population_raises_the_counts_for_background_morbidity():
    hv = recommend_safety_gating(study_type="biosimilar_pk", n_enrolled=60,
                                 healthy_volunteers=True)
    pts = recommend_safety_gating(study_type="biosimilar_pk", n_enrolled=60,
                                  healthy_volunteers=False)
    assert pts["counts"]["related_grade3_halt"] == hv["counts"]["related_grade3_halt"] + 1
    assert any(e["key"] == "patient_population" for e in pts["escalations"])
    assert "no therapeutic benefit" in hv["summary"].lower()


# ---------------------------------------------------------------------------
# Label-driven risk topics
# ---------------------------------------------------------------------------


def test_hepatotoxic_label_adds_the_hepatic_gate():
    label = {
        **ORAL_LABEL,
        "warnings_and_precautions": (
            "Severe hepatotoxicity, including fatal hepatic failure, has been reported. "
            "Elevations in ALT and AST occurred in 3 percent of patients."
        ),
    }
    out = recommend_safety_gating(study_type="BE", n_enrolled=24, label=label)
    assert "hepatotoxicity" in out["label_findings"]["risk_topics"]
    rule = next(r for r in out["individual_rules"] if r["key"] == "ind_hepatic_predose_gate")
    assert rule["added_because"], "a label-driven rule must say what it rests on"
    assert "24 and 48 hours" in rule["action"]


def test_insulin_label_adds_numeric_hypoglycaemia_gating():
    label = {
        "dosage_and_administration": "Administer subcutaneously once daily.",
        "warnings_and_precautions": (
            "Hypoglycemia is the most common adverse reaction of insulin therapy."
        ),
    }
    out = recommend_safety_gating(
        study_type="biosimilar_pk", design_key="crossover", n_enrolled=36, label=label)
    assert "hypoglycemia" in out["label_findings"]["risk_topics"]
    rule = next(r for r in out["individual_rules"] if r["key"] == "ind_hypoglycemia")
    assert rule["thresholds"]["level_2_mg_dl"] == 54.0
    assert "54 mg/dL" in rule["trigger"]


def test_unrelated_label_does_not_trigger_risk_rules():
    out = recommend_safety_gating(study_type="BE", n_enrolled=24, label=ORAL_LABEL)
    assert out["label_findings"]["risk_topics"] == []
    keys = {r["key"] for r in out["individual_rules"]}
    for key in ("ind_hepatic_predose_gate", "ind_qt_predose_gate", "ind_seizure",
                "ind_hypoglycemia", "ind_lactic_acidosis"):
        assert key not in keys


def test_no_label_does_not_crash_and_still_returns_the_core_rules():
    out = recommend_safety_gating(study_type="BE", n_enrolled=24, label=None)
    assert len(out["individual_rules"]) >= 5
    assert len(out["cohort_rules"]) >= 5
    assert out["summary"]


# ---------------------------------------------------------------------------
# Confinement discharge criteria
# ---------------------------------------------------------------------------


def test_discharge_requires_documented_physician_authorisation():
    out = recommend_safety_gating(study_type="BE", design_key="crossover", n_enrolled=24)
    discharge = out["discharge_criteria"]
    auth = next(c for c in discharge["criteria"] if c["key"] == "physician_authorisation")
    assert auth.get("mandatory") is True
    text = auth["criterion"].lower()
    assert "written authorisation" in text
    assert "signature" in text and "time" in text
    assert "authorisation" in discharge["authorisation"].lower()


def test_discharge_criteria_are_objective_and_cover_the_required_conditions():
    out = recommend_safety_gating(study_type="BE", design_key="crossover", n_enrolled=24)
    criteria = out["discharge_criteria"]["criteria"]
    keys = {c["key"] for c in criteria}
    for required in ("vital_signs", "ambulatory", "oral_intake",
                     "no_ongoing_related_grade2", "final_pk_sample",
                     "physician_authorisation"):
        assert required in keys, f"discharge criteria missing '{required}'"

    vitals = next(c for c in criteria if c["key"] == "vital_signs")
    assert vitals["thresholds"]["systolic_mmHg"] == [90, 140]
    assert vitals["thresholds"]["spo2_min_pct"] == 95
    assert "90-140 mmHg" in vitals["criterion"], "the range must be stated, not implied"

    ongoing = next(c for c in criteria if c["key"] == "no_ongoing_related_grade2")
    assert "grade 2" in ongoing["criterion"].lower()

    pk = next(c for c in criteria if c["key"] == "final_pk_sample")
    assert "final scheduled in-unit pharmacokinetic sample" in pk["criterion"].lower()


def test_discharge_ranges_are_narrower_than_the_stopping_limits():
    """A subject is stopped when something is wrong but discharged only when
    everything is right - inverting these would discharge unwell subjects."""
    th = recommend_safety_gating(study_type="BE", n_enrolled=24)["thresholds"]
    stop, disc = th["vital_sign_stop"], th["discharge_vitals"]
    assert disc["systolic_mmHg"][0] > stop["systolic_mmHg"][0]
    assert disc["systolic_mmHg"][1] < stop["systolic_mmHg"][1]
    assert disc["pulse_bpm"][0] > stop["pulse_bpm"][0]
    assert disc["pulse_bpm"][1] < stop["pulse_bpm"][1]
    assert disc["spo2_min_pct"] > stop["spo2_min_pct"]
    assert disc["temperature_max_c"] < stop["temperature_max_c"]


def test_parenteral_study_requires_post_injection_direct_observation():
    par = recommend_safety_gating(
        study_type="biosimilar_pk", design_key="parallel", n_enrolled=60,
        label=BIOLOGIC_LABEL)
    oral = recommend_safety_gating(study_type="BE", n_enrolled=24, label=ORAL_LABEL)
    par_keys = {c["key"] for c in par["discharge_criteria"]["criteria"]}
    oral_keys = {c["key"] for c in oral["discharge_criteria"]["criteria"]}
    assert "post_injection_observation" in par_keys
    assert "post_injection_observation" not in oral_keys
    obs = next(c for c in par["discharge_criteria"]["criteria"]
               if c["key"] == "post_injection_observation")
    assert obs["thresholds"]["direct_observation_hours"] == 4.0
    assert "resuscitation" in obs["criterion"].lower()


def test_failing_discharge_criteria_is_not_silently_a_withdrawal():
    out = recommend_safety_gating(study_type="BE", n_enrolled=24)
    text = out["discharge_criteria"]["if_not_met"].lower()
    assert "remains in the unit" in text
    assert "not by itself a withdrawal" in text


# ---------------------------------------------------------------------------
# Assumption disclosure
# ---------------------------------------------------------------------------


def test_every_assumed_number_is_disclosed_with_its_basis():
    out = recommend_safety_gating(study_type="biosimilar_pk", n_enrolled=48, is_nti=True)
    assumptions = out["assumptions"]
    assert assumptions
    keys = {a["key"] for a in assumptions}
    for required in ("related_grade3_halt_fraction", "discharge_vital_sign_ranges",
                     "min_post_dose_observation_hours", "vital_sign_action_limits"):
        assert required in keys
    for a in assumptions:
        assert a["value"] and a["basis"]
        assert len(a["basis"]) > 30, f"{a['key']}: a disclosure needs a real basis"


# ---------------------------------------------------------------------------
# Serialisation and the prompt directive
# ---------------------------------------------------------------------------


def test_result_is_json_serialisable_for_population_config():
    """It is persisted inside population_config (JSONB), so it must round-trip."""
    out = recommend_safety_gating(
        study_type="biosimilar_pk", design_key="parallel", n_enrolled=60,
        label={**BIOLOGIC_LABEL,
               "warnings_and_precautions": "Anaphylaxis and hepatotoxicity have occurred. "
                                           "See full prescribing information for complete "
                                           "boxed warning."},
        is_nti=True, dropout_rate=0.15, healthy_volunteers=False,
    )
    restored = json.loads(json.dumps(out))
    assert restored["counts"] == out["counts"]
    assert restored["individual_rules"][0]["key"] == out["individual_rules"][0]["key"]
    assert restored["discharge_criteria"]["criteria"][0]["criterion"]


def test_directive_locks_the_rules_for_the_prompt():
    out = recommend_safety_gating(study_type="BE", design_key="crossover", n_enrolled=24)
    directive = safety_gating_directive(out)
    assert "ALREADY DETERMINED" in directive
    # Both levels are named and kept apart.
    assert "INDIVIDUAL SUBJECT STOPPING RULES" in directive
    assert "COHORT / STUDY HALTING RULES" in directive
    assert "CONFINEMENT DISCHARGE CRITERIA" in directive
    # The load-bearing numbers survive into the prompt verbatim.
    assert "500 ms" in directive
    assert "60 ms" in directive
    assert "3 x the upper limit of normal" in directive
    assert str(out["counts"]["related_grade3_halt"]) in directive
    # Every rule made it in.
    for r in out["individual_rules"] + out["cohort_rules"]:
        assert r["label"] in directive
    # Empty input yields no directive rather than a misleading empty header.
    assert safety_gating_directive({}) == ""
    assert safety_gating_directive({"individual_rules": [], "cohort_rules": []}) == ""


def test_directive_contains_no_unrendered_placeholders():
    for kwargs in (
        {"study_type": "BE", "design_key": "crossover", "n_enrolled": 24},
        {"study_type": "biosimilar_pk", "design_key": "parallel", "n_enrolled": 60,
         "label": BIOLOGIC_LABEL},
        {"study_type": "BE", "is_nti": True},
    ):
        directive = safety_gating_directive(recommend_safety_gating(**kwargs))
        assert "{" not in directive and "}" not in directive, (
            f"unrendered placeholder leaked for {kwargs}")


def test_directive_tells_the_model_not_to_soften_the_numbers():
    """Numbers and the rule SET are inviolable; the WORDING is not.

    Asserted by intent rather than by exact phrasing, because the directive
    deliberately changed: it used to say "narrate them exactly as given", which
    made the model reproduce every ~90-word trigger verbatim and turned the
    safety section into pages of near-duplicate prose. What must never change is
    that no threshold is invented, rounded or dropped.
    """
    directive = safety_gating_directive(
        recommend_safety_gating(study_type="BE", n_enrolled=24))
    lowered = directive.lower()
    for demand in ("exactly", "never invent", "round", "omit"):
        assert demand in lowered, f"directive no longer forbids: {demand}"
    assert "assumption" in lowered, "unit conventions must be disclosed as such"


def test_directive_demands_concise_rendering():
    """The reviewer's finding was that the AE and stopping-criteria sections are
    far too long. The directive must actively ask for compression - an engine
    that tells the model to reproduce its source wording verbatim is the cause
    of the verbosity, not a neutral party to it."""
    directive = safety_gating_directive(
        recommend_safety_gating(study_type="BE", n_enrolled=24))
    lowered = directive.lower()
    assert "concisely" in lowered
    assert "one short sentence per cell" in lowered
    # CTCAE is a published scale; copying all five definitions is pure padding.
    assert "do not reproduce the full ctcae definitions" in lowered
