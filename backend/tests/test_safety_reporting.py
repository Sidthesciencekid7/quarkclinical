"""Tests for the expedited IND safety reporting engine (21 CFR 312.32).

These pin the errors that actually get protocols cited, not the shape of the
dict:

  * the three expedited criteria are a CONJUNCTION - an expected fatal reaction
    is not expedited-reportable, and neither is a serious unexpected event with
    no reasonable possibility of causation;
  * the clocks are CALENDAR days from the Sponsor's initial receipt, so a Friday
    receipt is due the following Friday;
  * an inconsistent input can never DOWNGRADE an obligation;
  * IND applicability is determined, not asserted - a domestic BE study in
    healthy volunteers is IND-exempt, a biosimilar PK study is not, and a
    cytotoxic label defeats the exemption.
"""
from __future__ import annotations

import itertools
import json

from services.safety_reporting import (
    CTGOV_REGISTRATION_DAYS,
    FATAL_OR_LIFE_THREATENING_DAYS,
    INVESTIGATOR_TO_SPONSOR_HOURS,
    IRB_NOTIFICATION_WORKING_DAYS,
    OTHER_SUSAR_DAYS,
    _EXPEDITED_CRITERIA,
    _SERIOUSNESS_CRITERIA,
    build_safety_reporting_plan,
    classify_event,
    decision_table,
    due_date_from_receipt,
    safety_reporting_directive,
)


# ---------------------------------------------------------------------------
# Catalogue integrity
# ---------------------------------------------------------------------------


def test_seriousness_criteria_are_the_regulation_s_own_list():
    keys = [c.key for c in _SERIOUSNESS_CRITERIA]
    assert len(keys) == len(set(keys)), "duplicate seriousness criterion keys"
    # All six outcome criteria of 21 CFR 312.32(a) must be present. Dropping the
    # important-medical-event catch-all is the classic under-reporting bug.
    for required in (
        "death", "life_threatening", "hospitalisation", "disability",
        "congenital_anomaly", "important_medical_event",
    ):
        assert required in keys, f"missing seriousness criterion: {required}"
    for crit in _SERIOUSNESS_CRITERIA:
        assert crit.label and crit.regulation_ref
        assert len(crit.definition) > 60, f"{crit.key} definition too thin"


def test_life_threatening_excludes_the_might_have_been_worse_reading():
    """312.32(a) explicitly excludes an event that MIGHT have caused death had
    it occurred in a more severe form - a very common protocol error."""
    lt = next(c for c in _SERIOUSNESS_CRITERIA if c.key == "life_threatening")
    assert "immediate" in lt.definition.lower()
    assert "more severe form" in lt.common_error.lower()


def test_severity_is_not_seriousness():
    plan = build_safety_reporting_plan("BE")
    text = plan["seriousness"]["severity_vs_seriousness"].lower()
    assert "ctcae" in text
    assert "intensity" in text


def test_expedited_criteria_are_exactly_three_independent_conditions():
    keys = [c.key for c in _EXPEDITED_CRITERIA]
    assert keys == ["serious", "unexpected", "causally_related"]
    unexpected = next(c for c in _EXPEDITED_CRITERIA if c.key == "unexpected")
    # The specificity/severity clause is the part most often dropped.
    assert "specificity" in unexpected.definition.lower()
    assert "severity" in unexpected.definition.lower()
    # A class-effect-only mention is UNEXPECTED for the specific product.
    assert "class effect" in unexpected.definition.lower()


# ---------------------------------------------------------------------------
# The decision engine - the clinically important part
# ---------------------------------------------------------------------------


def test_fatal_susar_is_seven_calendar_days():
    out = classify_event(True, True, True, True)
    assert out["expedited_ind_report_required"] is True
    assert out["deadline_days"] == FATAL_OR_LIFE_THREATENING_DAYS == 7
    assert out["deadline_unit"] == "calendar days"
    assert "312.32(c)(2)" in out["regulation_ref"]


def test_non_fatal_susar_is_fifteen_calendar_days():
    out = classify_event(True, True, True, False)
    assert out["expedited_ind_report_required"] is True
    assert out["deadline_days"] == OTHER_SUSAR_DAYS == 15
    assert "312.32(c)(1)" in out["regulation_ref"]


def test_expected_fatal_reaction_is_not_expedited():
    """The error this test exists for: a death is not automatically a 7-day
    report. If the reaction is listed in the IB at the observed severity it is
    EXPECTED, and no expedited IND safety report is due."""
    out = classify_event(True, False, True, True)
    assert out["is_susar"] is False
    assert out["expedited_ind_report_required"] is False
    assert out["deadline_days"] is None
    assert "unexpected" in out["failed_criteria"]
    # It is not thrown away: aggregate-rate reporting still applies.
    assert "312.32(c)(1)(iv)" in out["routine_handling"]


def test_serious_unexpected_but_unrelated_is_not_expedited():
    """No reasonable possibility of causation means it is not a suspected
    adverse reaction, so it is not expedited - the whole point of the 2010
    IND safety reporting rule."""
    out = classify_event(True, True, False, False)
    assert out["is_susar"] is False
    assert out["expedited_ind_report_required"] is False
    assert out["failed_criteria"] == ["causally_related"]
    assert "alternative cause" in out["routine_handling"]


def test_non_serious_event_is_never_expedited():
    out = classify_event(False, True, True, False)
    assert out["expedited_ind_report_required"] is False
    assert out["deadline_days"] is None


def test_serious_event_that_is_not_expedited_still_carries_duties():
    out = classify_event(True, False, True, False)
    handling = out["routine_handling"]
    assert str(INVESTIGATOR_TO_SPONSOR_HOURS) in handling
    assert "IRB" in handling


def test_inconsistent_input_never_downgrades_the_obligation():
    """Fatal implies serious by definition. A caller who passes serious=False
    with a fatal outcome must still get the 7-day report, plus a note."""
    out = classify_event(False, True, True, True)
    assert out["is_serious"] is True
    assert out["expedited_ind_report_required"] is True
    assert out["deadline_days"] == 7
    assert any("inconsistency" in n.lower() for n in out["notes"])
    # The raw inputs are preserved so the correction is auditable.
    assert out["inputs"]["serious"] is False


def test_truth_table_is_total_and_encodes_the_conjunction():
    """Every one of the sixteen combinations must classify, and expedited must
    hold if and only if all three criteria hold (with fatal implying serious)."""
    seen = 0
    for serious, unexpected, related, fatal in itertools.product(
        (True, False), repeat=4
    ):
        out = classify_event(serious, unexpected, related, fatal)
        seen += 1
        effective_serious = serious or fatal
        expected = effective_serious and unexpected and related
        assert out["expedited_ind_report_required"] is expected, (
            f"wrong obligation for serious={serious} unexpected={unexpected} "
            f"related={related} fatal={fatal}")
        if expected:
            assert out["deadline_days"] == (7 if fatal else 15)
        else:
            assert out["deadline_days"] is None
        # Total: always a usable result.
        assert out["report_type"]
        assert out["reasoning"]
        assert out["routine_handling"]
    assert seen == 16


def test_exactly_one_non_fatal_combination_triggers_a_report():
    """Pins the AND: of the eight (serious, unexpected, related) combinations,
    exactly one is expedited-reportable. An OR would give seven."""
    triggering = [
        (s, u, r)
        for s, u, r in itertools.product((True, False), repeat=3)
        if classify_event(s, u, r, False)["expedited_ind_report_required"]
    ]
    assert triggering == [(True, True, True)]


def test_decision_table_covers_every_coherent_combination():
    """The printed table drops the four logically impossible rows (fatal but
    not serious) and keeps the other twelve, of which exactly one is a 7-day
    report and exactly one is a 15-day report."""
    rows = decision_table(under_ind=True)
    assert len(rows) == 12
    keys = {(r["serious"], r["unexpected"], r["causally_related"],
             r["fatal_or_life_threatening"]) for r in rows}
    assert len(keys) == 12
    assert not any(r["fatal_or_life_threatening"] and not r["serious"] for r in rows)
    assert sum(1 for r in rows if r["deadline_days"] == 7) == 1
    assert sum(1 for r in rows if r["deadline_days"] == 15) == 1
    assert sum(1 for r in rows if r["expedited_ind_report_required"]) == 2


def test_decision_table_for_an_ind_exempt_study_files_nothing():
    rows = decision_table(under_ind=False)
    assert len(rows) == 12
    assert not any(r["expedited_ind_report_required"] for r in rows)
    assert all(r["deadline_days"] is None for r in rows)


# ---------------------------------------------------------------------------
# The clock - calendar days from sponsor awareness
# ---------------------------------------------------------------------------


def test_clock_starts_at_sponsor_initial_receipt_not_medical_review():
    out = classify_event(True, True, True, False)
    start = out["clock_start"].lower()
    assert "initial receipt" in start
    assert "aware" in start
    assert "database entry" in start, "must exclude the date the case is entered"


def test_deadlines_are_calendar_days_not_business_days():
    out = classify_event(True, True, True, True)
    note = out["calendar_day_note"].lower()
    assert "calendar" in note
    assert "weekend" in note
    assert "business day" not in note


def test_due_date_counts_weekends():
    """2026-07-03 is a Friday. Seven CALENDAR days later is Friday 2026-07-10 -
    not Tuesday 2026-07-14, which is what seven business days would give."""
    out = classify_event(True, True, True, True, initial_receipt_date="2026-07-03")
    assert out["due_date"] == "2026-07-10"
    fifteen = classify_event(
        True, True, True, False, initial_receipt_date="2026-07-03")
    assert fifteen["due_date"] == "2026-07-18"


def test_due_date_is_none_without_a_deadline_or_a_parsable_date():
    assert due_date_from_receipt("2026-07-03", None) is None
    assert due_date_from_receipt("not-a-date", 7) is None
    assert classify_event(True, False, True, False,
                          initial_receipt_date="2026-07-03")["due_date"] is None


# ---------------------------------------------------------------------------
# Regime applicability - honest, not universal
# ---------------------------------------------------------------------------


def test_biosimilar_pk_study_is_under_an_ind():
    plan = build_safety_reporting_plan("biosimilar_pk")
    assert plan["regime"]["under_ind"] is True
    assert "351(k)" in plan["regime"]["basis"] or "biosimilar" in plan["regime"]["basis"]
    assert all(d["applicable"] for d in plan["timelines"])


def test_domestic_be_study_in_healthy_volunteers_is_ind_exempt():
    """A conventional ANDA BE study is frequently NOT under an IND; asserting
    IND obligations universally is the error this encodes against."""
    plan = build_safety_reporting_plan("BE", healthy_volunteers=True)
    regime = plan["regime"]
    assert regime["under_ind"] is False
    assert "320.31" in regime["regime"] or "320.31" in regime["basis"]
    # The 312.32 duties are still WRITTEN, but explicitly marked inapplicable.
    assert plan["timelines"], "the plan must still state the IND duties"
    assert not any(d["applicable"] for d in plan["timelines"])
    for duty in plan["timelines"]:
        assert "IND-exempt" in duty["applies_when"] or "does not apply" in duty["applies_when"]
    # IRB and consent obligations survive the exemption.
    assert "part 56" in regime["basis"] or "56" in regime["basis"]


def test_cytotoxic_label_text_defeats_the_be_ind_exemption():
    """Label-grounded, not model recall: 21 CFR 320.31 excludes cytotoxic drug
    products from the BA/BE exemption."""
    label = {
        "clinical_pharmacology": (
            "Doxorubicin is a cytotoxic anthracycline antibiotic. Severe "
            "myelosuppression occurs in most patients."
        )
    }
    plan = build_safety_reporting_plan("BE", label=label)
    regime = plan["regime"]
    assert regime["under_ind"] is True
    assert regime["exemption_available"] is False
    cyto = next(c for c in regime["exemption_conditions"] if c["key"] == "not_cytotoxic")
    assert cyto["met"] is False
    assert "cytotoxic" in cyto["evidence"].lower()
    assert all(d["applicable"] for d in plan["timelines"])


def test_clean_be_label_keeps_the_exemption():
    label = {"pharmacokinetics": "Absorption is rapid; 90% is excreted unchanged in urine."}
    plan = build_safety_reporting_plan("BE", label=label)
    assert plan["regime"]["under_ind"] is False
    assert plan["regime"]["exemption_available"] is True


def test_explicit_is_ind_override_wins_and_says_so():
    plan = build_safety_reporting_plan("BE", True)
    assert plan["regime"]["under_ind"] is True
    assert "caller" in plan["regime"]["determined_by"].lower()
    assert all(d["applicable"] for d in plan["timelines"])


def test_new_chemical_entity_be_study_requires_an_ind():
    plan = build_safety_reporting_plan("BE", is_new_chemical_entity=True)
    assert plan["regime"]["under_ind"] is True
    nce = next(c for c in plan["regime"]["exemption_conditions"]
               if c["key"] == "not_new_chemical_entity")
    assert nce["met"] is False


def test_declaring_no_ind_does_not_fabricate_an_exemption():
    """A caller can declare 'no IND' - regulatory affairs owns that call - but
    the engine must not then claim the 320.31(d) exemption it could not
    establish."""
    plan = build_safety_reporting_plan("biosimilar_pk", False)
    regime = plan["regime"]
    assert regime["under_ind"] is False
    assert regime["exemption_available"] is False
    assert "NOT established" in regime["regime"]
    assert "regulatory affairs" in regime["basis"].lower()


def test_summary_never_claims_an_exemption_the_engine_did_not_establish():
    """The summary is the line the generation prompt narrates, so it must agree
    with the regime block. A caller-declared 'no IND' on a study whose 320.31(d)
    exemption could NOT be established must not be summarised as exempt - that
    is the exact false claim this engine exists to prevent, and it would be
    narrated into the protocol verbatim."""
    declared = build_safety_reporting_plan("biosimilar_pk", False)
    assert declared["regime"]["exemption_available"] is False
    summary = declared["summary"]
    assert "320.31(d)" not in summary or "does NOT establish" in summary
    assert "is exempt from 21 CFR part 312" not in summary
    assert "regulatory affairs" in summary.lower()

    # The genuinely exempt BE study still says so plainly - the fix must not
    # hedge a determination the engine DID make.
    exempt = build_safety_reporting_plan("BE")
    assert exempt["regime"]["exemption_available"] is True
    assert "exempt from 21 CFR part 312 under 21 CFR 320.31(d)" in exempt["summary"]

    # And an IND study is never described as exempt either way.
    ind = build_safety_reporting_plan("biosimilar_pk")
    assert "exempt" not in ind["summary"].lower()


def test_unrecognised_study_type_defaults_to_the_conservative_regime():
    """Never claim an exemption that cannot be established - default to IND and
    say the determination needs confirming."""
    plan = build_safety_reporting_plan("some_new_study_type")
    regime = plan["regime"]
    assert regime["under_ind"] is True
    assert regime["exemption_available"] is False
    assert "conservative" in regime["basis"].lower()
    keys = {c["key"] for c in regime["exemption_conditions"]}
    assert "study_type_unrecognised" in keys
    assert "biological_product" not in keys, "must not borrow the biosimilar rationale"


def test_non_ind_susar_keeps_the_classification_but_files_no_fda_report():
    out = classify_event(True, True, True, True, under_ind=False)
    assert out["is_susar"] is True
    assert out["expedited_ind_report_required"] is False
    assert out["deadline_days"] is None
    assert "7 calendar day" in out["would_be_report_type"]
    assert "IRB" in " ".join(out["notes"]) or "IRB" in out["routine_handling"]


# ---------------------------------------------------------------------------
# Parallel obligations
# ---------------------------------------------------------------------------


def test_plan_covers_investigator_irb_and_sponsor_notifications():
    plan = build_safety_reporting_plan("biosimilar_pk", org_name="Acme Bio")
    keys = {d["key"] for d in plan["parallel_obligations"]}
    assert "investigator_to_sponsor" in keys
    assert "sponsor_to_investigators" in keys
    assert "irb_notification" in keys
    assert "pregnancy_reporting" in keys
    assert plan["sponsor"] == "Acme Bio"


def test_sponsor_notifies_all_participating_investigators_on_the_15_day_clock():
    plan = build_safety_reporting_plan("biosimilar_pk")
    duty = next(d for d in plan["parallel_obligations"]
                if d["key"] == "sponsor_to_investigators")
    assert duty["deadline_value"] == OTHER_SUSAR_DAYS
    assert "investigator" in duty["recipient"].lower()


def test_investigator_reports_every_sae_not_only_the_reportable_ones():
    """The site must not pre-filter on expectedness or causality - that is the
    Sponsor's determination, and filtering at the site loses cases."""
    plan = build_safety_reporting_plan("BE")
    duty = next(d for d in plan["parallel_obligations"]
                if d["key"] == "investigator_to_sponsor")
    assert duty["applicable"] is True, "applies even to an IND-exempt study"
    assert "irrespective" in duty["trigger"].lower()
    assert duty["deadline_value"] == INVESTIGATOR_TO_SPONSOR_HOURS


def test_open_label_be_study_has_no_unblinding_step():
    be = build_safety_reporting_plan("BE")
    unblind = next(d for d in be["parallel_obligations"]
                   if d["key"] == "unblinding_for_report")
    assert unblind["applicable"] is False
    bio = build_safety_reporting_plan("biosimilar_pk")
    unblind_bio = next(d for d in bio["parallel_obligations"]
                       if d["key"] == "unblinding_for_report")
    assert unblind_bio["applicable"] is True


# ---------------------------------------------------------------------------
# ClinicalTrials.gov (FDAAA 801)
# ---------------------------------------------------------------------------


def test_be_study_is_not_an_applicable_clinical_trial():
    """The determination is still MADE and still auditable - it just carries no
    prose, because a protocol does not explain why a regulation it is not
    subject to does not apply to it."""
    ct = build_safety_reporting_plan("BE")["clinicaltrials_gov"]
    assert ct["is_applicable_clinical_trial"] is False
    assert ct["duties"] == []
    assert ct["determined_by"], "the determination must stay auditable"
    # No sentence for the narration layer to lift into the document.
    assert ct["state_in_protocol"] is False
    assert ct["rationale"] == ""
    assert ct["voluntary_note"] == ""


def test_non_applicable_trial_emits_no_ctgov_text_anywhere_in_the_plan():
    """The exact passage a reviewing clinical pharmacologist struck:

        "Phase 1 investigations of drug products ... are excluded from the
         definition of an applicable clinical trial in 42 CFR 11.10, so FDAAA
         801 registration and results submission are not required for this
         study."

    It reached the protocol twice - once through the plan summary and once
    through the directive's ClinicalTrials.gov line. Neither may carry it now,
    and no substitute sentence may take its place.
    """
    for study_type in ("BE", "food_effect", "biosimilar_pk"):
        plan = build_safety_reporting_plan(study_type, healthy_volunteers=True)
        assert plan["clinicaltrials_gov"]["state_in_protocol"] is False

        summary = plan["summary"].lower()
        for banned in ("clinicaltrials", "fdaaa", "42 cfr 11",
                       "applicable clinical trial", "registration"):
            assert banned not in summary, (
                f"{study_type}: summary still mentions '{banned}' - the summary "
                "is narrated verbatim into the protocol")

        # The rendered plan itself must not carry the paragraph either: it is
        # serialised into the prompt as JSON alongside the directive.
        blob = json.dumps(plan["clinicaltrials_gov"]).lower()
        assert "42 cfr 11.10" not in blob
        assert "not required" not in blob


def test_directive_suppresses_ctgov_instead_of_explaining_the_exclusion():
    directive = safety_reporting_directive(build_safety_reporting_plan("BE"))
    # The model is told, in the prompt, to write nothing about it. That line is
    # an INSTRUCTION, so it is the only place these terms may still appear.
    assert "PUBLIC TRIAL REGISTRY - WRITE NOTHING" in directive
    assert "not even a sentence" in directive
    suppression = next(
        line for line in directive.splitlines()
        if "PUBLIC TRIAL REGISTRY" in line
    )
    rest = directive.replace(suppression, "")

    # Nothing narratable is left anywhere else in the directive.
    for banned in ("ClinicalTrials.gov", "FDAAA", "42 CFR 11", "42 CFR part 11",
                   "applicable clinical trial", "registration"):
        assert banned not in rest, f"directive still offers '{banned}' to narrate"
    # And the struck sentence itself is gone in every wording.
    assert "excluded from the definition" not in directive
    assert "are not required for this study" not in directive


def test_patient_biosimilar_study_is_an_applicable_clinical_trial():
    ct = build_safety_reporting_plan(
        "biosimilar_pk", healthy_volunteers=False)["clinicaltrials_gov"]
    assert ct["is_applicable_clinical_trial"] is True
    assert ct["state_in_protocol"] is True
    reg = next(d for d in ct["duties"] if d["key"] == "ctgov_registration")
    assert reg["deadline_value"] == CTGOV_REGISTRATION_DAYS == 21
    results = next(d for d in ct["duties"] if d["key"] == "ctgov_results")
    assert results["deadline_value"] == 365


def test_a_real_registration_obligation_still_reaches_the_protocol():
    """Removing the noise must not remove the signal: where registration IS
    required it is a duty of this protocol and must be stated with its clock."""
    plan = build_safety_reporting_plan(
        "biosimilar_pk", healthy_volunteers=False)
    assert "applicable clinical trial" in plan["summary"].lower()
    assert f"{CTGOV_REGISTRATION_DAYS} days" in plan["summary"]

    directive = safety_reporting_directive(plan)
    assert "ClinicalTrials.gov:" in directive
    assert "Register the trial on ClinicalTrials.gov" in directive
    assert "42 CFR 11.24(a)" in directive
    assert "WRITE NOTHING" not in directive


def test_ctgov_override_is_respected():
    ct = build_safety_reporting_plan(
        "BE", is_applicable_clinical_trial=True)["clinicaltrials_gov"]
    assert ct["is_applicable_clinical_trial"] is True
    assert ct["state_in_protocol"] is True
    assert ct["determined_by"] == "caller override"


# ---------------------------------------------------------------------------
# Label-grounded expectedness and signals
# ---------------------------------------------------------------------------


def test_labelled_serious_risks_become_the_expectedness_baseline():
    label = {
        "clinical_pharmacology": (
            "Hepatotoxicity has been reported, with transaminase elevations. "
            "QT prolongation was observed at supratherapeutic doses."
        )
    }
    plan = build_safety_reporting_plan("BE", label=label)
    risks = {r["key"] for r in plan["expectedness_reference"]["labelled_serious_risks"]}
    assert "hepatotoxicity" in risks
    assert "qt_prolongation" in risks
    rule = plan["expectedness_reference"]["rule"].lower()
    assert "specificity" in rule and "severity" in rule
    # Evidence is the actual label phrase, so a reviewer can find it.
    hep = next(s for s in plan["safety_signals"] if s["key"] == "hepatotoxicity")
    assert hep["evidence"] in label["clinical_pharmacology"].lower()


def test_unrelated_label_produces_no_spurious_signals():
    label = {"pharmacokinetics": "Absorption is rapid and distribution is wide."}
    plan = build_safety_reporting_plan("BE", label=label)
    assert plan["safety_signals"] == []
    assert plan["expectedness_reference"]["labelled_serious_risks"] == []


def test_no_label_does_not_crash_and_still_returns_a_full_plan():
    plan = build_safety_reporting_plan("biosimilar_pk", label=None)
    assert plan["timelines"] and plan["parallel_obligations"]
    assert plan["summary"]
    assert plan["expectedness_reference"]["label_text_available"] is False


def test_teratogenic_label_drives_the_pregnancy_pathway():
    label = {"indications_usage": "May cause fetal harm; embryo-fetal toxicity was observed."}
    plan = build_safety_reporting_plan("BE", label=label)
    keys = {s["key"] for s in plan["safety_signals"]}
    assert "embryofetal_toxicity" in keys


# ---------------------------------------------------------------------------
# Assumptions, cross-references, serialisation, directive
# ---------------------------------------------------------------------------


def test_assumed_numbers_are_flagged_and_disclosed():
    """Numbers we chose (no figure in the regulation) must be surfaced so the
    protocol discloses them rather than passing them off as regulatory text."""
    plan = build_safety_reporting_plan("biosimilar_pk")
    assumed_keys = {a["key"] for a in plan["assumptions"] if a["disclose_in_protocol"]}
    assert "irb_notification_working_days" in assumed_keys
    assert "investigator_to_sponsor_hours" in assumed_keys
    assert "clock_start_convention" in assumed_keys
    irb = next(d for d in plan["parallel_obligations"] if d["key"] == "irb_notification")
    assert irb["assumed"] is True
    assert irb["deadline_value"] == IRB_NOTIFICATION_WORKING_DAYS
    # The cited figures must NOT be flagged as assumptions.
    susar15 = next(d for d in plan["timelines"] if d["key"] == "susar_15_day")
    assert susar15["assumed"] is False


def test_ema_guideline_adds_the_parallel_eu_obligation():
    plan = build_safety_reporting_plan("BE", guideline_version="EMA-2010")
    regions = {c["region"] for c in plan["regional_cross_reference"]}
    assert "European Union" in regions
    eu = next(c for c in plan["regional_cross_reference"] if c["region"] == "European Union")
    assert "EudraVigilance" in eu["obligation"]

    # It reaches the prompt, flagged as a PARALLEL obligation so the model
    # cannot merge the EU clocks into the FDA ones.
    directive = safety_reporting_directive(plan)
    assert "PARALLEL REGIONAL OBLIGATIONS" in directive
    assert "EudraVigilance" in directive

    fda_only = build_safety_reporting_plan("BE", guideline_version="FDA-2021")
    assert fda_only["regional_cross_reference"] == []
    assert "EudraVigilance" not in safety_reporting_directive(fda_only)


def test_plan_is_json_serialisable_for_population_config():
    """It is persisted inside population_config (JSONB), so it must round-trip."""
    plan = build_safety_reporting_plan(
        "biosimilar_pk",
        org_name="Acme Bio",
        label={"clinical_pharmacology": "Anaphylaxis and hepatotoxicity have been reported."},
        guideline_version="ICH-M13A",
    )
    restored = json.loads(json.dumps(plan))
    assert restored["regime"]["under_ind"] is True
    assert restored["decision_table"][0]["report_type"]
    assert restored["safety_signals"][0]["key"] == plan["safety_signals"][0]["key"]


def test_classify_event_result_is_json_serialisable():
    out = classify_event(True, True, True, True, initial_receipt_date="2026-07-03")
    restored = json.loads(json.dumps(out))
    assert restored["deadline_days"] == 7
    assert restored["due_date"] == "2026-07-10"


def test_directive_locks_the_plan_for_the_prompt():
    plan = build_safety_reporting_plan("biosimilar_pk", org_name="Acme Bio")
    directive = safety_reporting_directive(plan)
    assert "ALREADY DETERMINED" in directive
    # The conjunction and both clocks must be in the locked block.
    assert "AND" in directive
    assert "7 calendar days" in directive
    assert "15 calendar days" in directive
    assert "initial receipt" in directive.lower()
    # Every seriousness criterion is handed to the model.
    for crit in _SERIOUSNESS_CRITERIA:
        assert crit.label in directive
    # Empty input yields no directive rather than a misleading empty header.
    assert safety_reporting_directive({}) == ""
    assert safety_reporting_directive({"regime": None}) == ""


def test_directive_for_an_ind_exempt_study_does_not_assert_ind_duties():
    plan = build_safety_reporting_plan("BE")
    directive = safety_reporting_directive(plan)
    assert "DOES NOT APPLY TO THIS STUDY" in directive
    assert "IND-exempt" in directive
    # The site/IRB duties that DO survive the exemption are still instructed.
    assert "Investigator notification" in directive


def test_directive_is_ascii_safe_for_a_windows_console():
    """The repo has been bitten by cp1252 encode errors on print/log output."""
    for plan in (build_safety_reporting_plan("BE"),
                 build_safety_reporting_plan("biosimilar_pk")):
        safety_reporting_directive(plan).encode("ascii")
        json.dumps(plan).encode("ascii")
