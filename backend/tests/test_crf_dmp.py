"""Tests for the CRF specification / Data Management Plan engine.

The engine is a deterministic knowledge layer: given a design, a sampling
schedule and the real label text, it must return a stable, defensible
collection instrument. These tests pin the things whose regression would be a
REAL clinical failure rather than a cosmetic one:

* the PK sample log must carry the ACTUAL draw time and derive elapsed time
  from it - a specification that fed the NOMINAL time to the analysis would
  produce silently biased AUC and lambda_z that no re-analysis can repair;
* a post-dose draw must be impossible to save with a timestamp before its dose;
* the CRF must be a genuine superset of what services/dataset_ingest and
  services/nca need, checked against those modules live rather than by eye;
* the specification must not CLAIM to collect the bioanalytical concentrations,
  which arrive as an external transfer;
* design-awareness (a parallel study has one period and no washout) and
  label-driven applicability (a hepatotoxic drug gets a Hy's Law form) must not
  drift into a generic template.

and, separately from what the engine COMPUTES, what the PROTOCOL is allowed to
QUOTE. A practising clinical pharmacologist reviewing a generated protocol asked
for the field-level CRF specification - the dosing log's critical fields (EX),
the PK sample collection log's critical fields (PC), the adverse event critical
fields (AE) and the pre-printed PK sample log rows - to come OUT of the protocol
and live in the separate SAP/CRF-specification document. The default directive
scope is therefore "protocol", and the tests below pin BOTH halves of that
contract: the protocol block must not carry the tables, and the engine must
still produce them for the document that will.
"""
from __future__ import annotations

import json

import pytest

from services.dataset_ingest import REQUIRED_COLUMNS as _ANALYSIS_REQUIRED
from services.crf_dmp import (
    ARCHIVE_RETENTION_YEARS,
    EMESIS_EXCLUSION_TMAX_MULTIPLE,
    OVERNIGHT_FAST_HOURS,
    _CATALOGUE,
    _RELATED_TRUE_VALUES,
    _STATIC_CHECKS,
    build_crf_spec,
    build_data_management_plan,
    crf_dmp_directive,
)


# ---------------------------------------------------------------------------
# Fixtures - a realistic sampling schedule, mirroring the shape produced by
# services.sampling_schedule.compute_sampling_schedule(). Built here rather
# than imported so this test exercises the CRF engine alone.
# ---------------------------------------------------------------------------


def _timepoint(index, hours, phase, window_minutes, day=1):
    return {
        "index": index,
        "hours": hours,
        "day": day,
        "label": f"{hours} h",
        "phase": phase,
        "lambda_z_point": phase == "elimination" and hours >= 24.0,
        "window_minutes": window_minutes,
        "window": f"+/- {window_minutes} min",
        "volume_ml": 4.0,
        "cumulative_volume_ml": 4.0 * index,
        "purpose": "test fixture",
    }


def _sampling(last_hours=72.0, tmax=2.0, periods=2):
    # (hours, phase, window_minutes) - truncated at the requested horizon so a
    # short-schedule fixture really is short.
    base = [
        (0.0, "pre-dose", 60), (0.5, "absorption", 5), (1.0, "peak", 5),
        (2.0, "peak", 5), (4.0, "peak", 15), (8.0, "elimination", 15),
        (24.0, "elimination", 60), (48.0, "elimination", 60),
        (72.0, "elimination", 60),
    ]
    times = [t for t in base if t[0] <= last_hours]
    if times[-1][0] < last_hours:
        times.append((last_hours, "elimination", 60))
    import math as _math
    tps = [
        _timepoint(i, hours, phase, window,
                   day=max(1, int(_math.ceil(hours / 24.0)) or 1))
        for i, (hours, phase, window) in enumerate(times, 1)
    ]
    return {
        "timepoints": tps,
        "n_timepoints": len(tps),
        "last_timepoint_hours": last_hours,
        "tmax_hours": tmax,
        "t_half_hours": 12.0,
        "periods": periods,
        "design_key": "crossover",
        "is_biologic": False,
    }


QT_LABEL = {
    "warnings_and_precautions": (
        "Dose-dependent QT interval prolongation has been observed. Cases of "
        "torsades de pointes have been reported."
    )
}
HEPATOTOX_LABEL = {
    "warnings_and_precautions": (
        "Hepatotoxicity, including cases of drug-induced liver injury with "
        "marked transaminase elevation, has been reported."
    )
}
BLAND_LABEL = {
    "clinical_pharmacology": (
        "Absorption is rapid and the drug is widely distributed. Elimination is "
        "primarily by glucuronidation."
    )
}


# ---------------------------------------------------------------------------
# Catalogue integrity
# ---------------------------------------------------------------------------


def test_form_catalogue_is_well_formed():
    keys = [f.key for f in _CATALOGUE]
    assert len(keys) == len(set(keys)), "duplicate form keys"
    for form in _CATALOGUE:
        assert form.title and form.purpose and form.completed_at
        assert form.sdtm_domain, f"{form.key} has no CDISC domain mapping"
        assert form.repeating in (
            "once", "per_period", "per_visit", "per_record", "per_sample")
        # Rationale must be a real justification, not a stub.
        assert len(form.rationale) > 80, f"{form.key} rationale too thin"
        assert form.fields, f"{form.key} has no fields"
        names = [fl.name for fl in form.fields]
        assert len(names) == len(set(names)), f"duplicate field names in {form.key}"
        for fl in form.fields:
            assert fl.dtype in (
                "text", "integer", "decimal", "date", "time", "datetime",
                "codelist", "boolean"), f"{form.key}.{fl.name} bad dtype"
            assert fl.label, f"{form.key}.{fl.name} has no label"
            assert fl.permitted_text(), f"{form.key}.{fl.name} has no permitted values"
            if fl.dtype == "codelist":
                assert fl.codelist, f"{form.key}.{fl.name} is a codelist with no values"
            if fl.derived:
                assert fl.derivation, (
                    f"{form.key}.{fl.name} is derived but does not say how - a "
                    f"derivation nobody can reproduce is not a specification")


def test_every_mandated_form_is_present():
    """The protocol package is incomplete without any one of these."""
    required = {
        "dm_demographics", "ie_eligibility", "mh_medical_history",
        "cm_concomitant_medications", "ex_dosing", "pc_pk_sample_log",
        "vs_vital_signs", "eg_ecg", "lb_laboratory", "ae_adverse_events",
        "ds_study_completion",
    }
    assert required <= {f.key for f in _CATALOGUE}


def test_edit_check_catalogue_is_well_formed():
    keys = [c.key for c in _STATIC_CHECKS]
    assert len(keys) == len(set(keys)), "duplicate edit check keys"
    for c in _STATIC_CHECKS:
        assert c.severity in ("hard", "query", "review")
        assert c.check_type in ("range", "cross_form", "missing", "consistency",
                                "uniqueness", "reconciliation")
        assert c.forms and c.fields and c.rule and c.action
        assert len(c.rationale) > 60, f"{c.key} rationale too thin"


# ---------------------------------------------------------------------------
# THE analysis-integrity invariants - the reason this module exists
# ---------------------------------------------------------------------------


def _field(spec, form_key, field_name):
    form = next(f for f in spec["forms"] if f["key"] == form_key)
    return next(fl for fl in form["fields"] if fl["name"] == field_name)


def test_pk_log_captures_actual_draw_time_not_only_nominal():
    """A log that records only the nominal time produces unanalysable data."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    form = next(f for f in spec["forms"] if f["key"] == "pc_pk_sample_log")
    names = {fl["name"] for fl in form["fields"]}
    assert {"nominal_time_h", "actual_draw_date", "actual_draw_time",
            "elapsed_time_h", "deviation_minutes", "deviation_reason"} <= names

    actual_date = _field(spec, "pc_pk_sample_log", "actual_draw_date")
    actual_time = _field(spec, "pc_pk_sample_log", "actual_draw_time")
    assert actual_date["required"] and actual_time["required"]
    assert actual_time["dtype"] == "time"


def test_nca_time_column_comes_from_elapsed_not_nominal_time():
    """The single most consequential mapping in the whole specification.

    services/nca.py integrates concentration against ELAPSED time. If the
    nominal timepoint were mapped to the analysis 'time' column, every late or
    early draw would be analysed as though it had been on schedule and the AUC
    error would be undetectable afterwards.
    """
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    elapsed = _field(spec, "pc_pk_sample_log", "elapsed_time_h")
    nominal = _field(spec, "pc_pk_sample_log", "nominal_time_h")
    assert "nca.time" in elapsed["analysis_columns"]
    assert "nca.time" not in nominal["analysis_columns"], (
        "the nominal time must NEVER be the analysis time")
    assert elapsed["derived"] and elapsed["derivation"]
    assert "dose" in elapsed["derivation"].lower()


def test_draw_before_dose_is_a_hard_block_not_a_query():
    """A post-dose sample timestamped before its dose must not be savable."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    check = next(c for c in spec["edit_checks"] if c["key"] == "pk_draw_after_dose")
    assert check["severity"] == "hard", (
        "a mis-timestamped draw silently corrupts the profile; a query that the "
        "site can answer 'confirmed' is not enough")
    assert check["check_type"] == "cross_form"
    assert "ex_dosing" in check["forms"] and "pc_pk_sample_log" in check["forms"]


def test_dose_clock_time_is_mandatory_and_the_dosing_form_is_critical():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    dose_time = _field(spec, "ex_dosing", "dose_time")
    assert dose_time["required"] and dose_time["dtype"] == "time"
    assert "ex_dosing" in spec["critical_forms"]
    assert "pc_pk_sample_log" in spec["critical_forms"]


def test_pk_sample_log_is_preprinted_from_the_locked_schedule():
    sampling = _sampling()
    spec = build_crf_spec(design_key="crossover", sampling=sampling)
    assert len(spec["pk_sample_log_rows"]) == len(sampling["timepoints"])
    assert [r["nominal_time_h"] for r in spec["pk_sample_log_rows"]] == [
        t["hours"] for t in sampling["timepoints"]]
    assert all(r["repeated_in_periods"] == spec["periods"]
               for r in spec["pk_sample_log_rows"])
    # And the window check quotes this study's real windows.
    window_check = next(c for c in spec["edit_checks"]
                        if c["key"] == "pk_draw_within_window")
    assert "5 min" in window_check["rule"]


def test_missing_sampling_schedule_is_flagged_not_invented():
    spec = build_crf_spec(design_key="crossover", sampling=None)
    assert spec["pk_sample_log_rows"] == []
    keys = {f["key"] for f in spec["flags"]}
    assert "sampling_schedule_not_supplied" in keys
    assert spec["must_disclose"] is True
    # The form itself still exists - only the pre-printed rows are missing.
    assert any(f["key"] == "pc_pk_sample_log" for f in spec["forms"])


@pytest.mark.parametrize("tmax,expected_window", [(3.0, "6 h"), (1.5, "3 h"),
                                                 (0.75, "1.5 h")])
def test_emesis_check_uses_two_times_this_drugs_real_tmax(tmax, expected_window):
    """Exclusion for emesis turns on 2 x median Tmax, so the window has to be
    computed from THIS drug's Tmax, not stated generically."""
    assert EMESIS_EXCLUSION_TMAX_MULTIPLE == 2.0
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(tmax=tmax))
    check = next(c for c in spec["edit_checks"]
                 if c["key"] == "emesis_within_exclusion_window")
    assert expected_window in check["rule"]
    assert f"{tmax:g} h" in check["rule"], "the Tmax it was derived from must be cited"


def test_fast_duration_check_uses_the_guidance_figure():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    check = next(c for c in spec["edit_checks"] if c["key"] == "fast_duration_adequate")
    assert str(int(OVERNIGHT_FAST_HOURS)) in check["rule"]


# ---------------------------------------------------------------------------
# The bridge to this product's own downstream pipeline
# ---------------------------------------------------------------------------


def test_crf_is_a_superset_of_the_analysis_upload_contract():
    """Checked against services/dataset_ingest LIVE, not against a copy.

    If a required column is ever added to the upload contract and not to the
    CRF, this fails - which is the point: a study run on a CRF that misses a
    required column cannot be analysed by this product at all.
    """
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    cov = spec["downstream_analysis_coverage"]
    assert cov["uncovered_required_columns"] == []
    assert cov["is_superset"] is True

    for kind, required in _ANALYSIS_REQUIRED.items():
        covered = {e["column"] for e in cov["by_kind"][kind] if e["covered"]}
        missing = set(required) - covered
        assert not missing, f"{kind}: uncovered required columns {sorted(missing)}"


def test_concentration_is_declared_external_not_claimed_as_a_crf_field():
    """The CRF does not collect concentrations - the laboratory produces them.

    Claiming otherwise would describe a process that does not exist and would
    hide the Data Transfer Agreement the study actually depends on.
    """
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    entry = next(e for e in spec["downstream_analysis_coverage"]["by_kind"]["nca"]
                 if e["column"] == "concentration")
    assert entry["covered"] is True
    assert entry["source"] == "external"
    assert not entry["form"], "no CRF form may claim to collect concentrations"
    assert "blq" in entry["note"].lower(), (
        "the transfer must state the BLQ convention, since BLQ is missing, not zero")


def test_derived_pk_parameters_are_attributed_to_the_nca_engine():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    params = {e["column"]: e for e in
              spec["downstream_analysis_coverage"]["by_kind"]["params"]}
    for col in ("cmax", "auc0t", "auc0inf"):
        assert params[col]["source"] == "derived"
        assert "non-compartmental" in params[col]["note"].lower()


def test_ae_start_day_derivation_states_the_no_day_zero_convention():
    """ae.start_day is an integer study day; getting the convention wrong shifts
    every treatment-emergent classification by one day."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    start_day = _field(spec, "ae_adverse_events", "start_day")
    assert "ae.start_day" in start_day["analysis_columns"]
    assert start_day["derived"] is True
    assert "no day 0" in start_day["derivation"].lower()


def test_ae_related_collapse_rule_is_prespecified_and_conservative():
    """ae.related is a boolean but causality is a five-point scale; the collapse
    must be fixed in advance, and 'unlikely related' must not count as related."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    related = _field(spec, "ae_adverse_events", "related")
    assert related["derived"] is True
    assert "ae.related" in related["analysis_columns"]
    for value in _RELATED_TRUE_VALUES:
        assert value in related["derivation"]
    assert "Not related" not in related["derivation"]
    assert "Unlikely related" not in related["derivation"]


# ---------------------------------------------------------------------------
# Design awareness - the clinically important part
# ---------------------------------------------------------------------------


def test_parallel_design_has_one_period_no_washout_and_no_period_disposition():
    spec = build_crf_spec(design_key="parallel", sampling=None, t_half_hours=12.0)
    assert spec["periods"] == 1
    assert spec["washout_days"] is None, "a parallel study has no washout"
    assert "multi_period" not in spec["active_conditions"]
    assert "ds_period_disposition" not in {f["key"] for f in spec["forms"]}
    assert not any(v["epoch"] == "WASHOUT" for v in spec["visit_schedule"])
    assert spec["sequences"] == ["T", "R"]


def test_crossover_repeats_the_dosing_and_pk_forms_per_period_with_a_washout():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(),
                          t_half_hours=12.0)
    assert spec["periods"] == 2
    # 7 x 12 h / 24 = 3.5 days, the same arithmetic the study design locks.
    assert spec["washout_days"] == pytest.approx(3.5)
    assert spec["sequences"] == ["TR", "RT"]
    dosing = next(f for f in spec["forms"] if f["key"] == "ex_dosing")
    assert dosing["repeating"] == "per_period" and dosing["repeats"] == 2
    assert "ds_period_disposition" in {f["key"] for f in spec["forms"]}
    assert any(v["epoch"] == "WASHOUT" for v in spec["visit_schedule"])


def test_replicate_design_gets_four_periods_and_replicate_sequences():
    spec = build_crf_spec(design_key="replicate", sampling=None, t_half_hours=6.0)
    assert spec["periods"] == 4
    assert spec["sequences"] == ["TRTR", "RTRT"]
    washout_visits = [v for v in spec["visit_schedule"] if v["epoch"] == "WASHOUT"]
    assert len(washout_visits) == 3, "n periods means n-1 washouts"


def test_period_range_check_matches_the_design():
    xo = build_crf_spec(design_key="crossover", sampling=_sampling())
    par = build_crf_spec(design_key="parallel", sampling=None)
    xo_rule = next(c for c in xo["edit_checks"]
                   if c["key"] == "period_within_design")["rule"]
    par_rule = next(c for c in par["edit_checks"]
                    if c["key"] == "period_within_design")["rule"]
    assert "between 1 and 2" in xo_rule
    assert "between 1 and 1" in par_rule


def test_visit_schedule_splits_in_house_from_ambulatory_draws():
    """A 72 h schedule cannot keep healthy volunteers confined for three days;
    the later draws are ambulatory visits and the CRF must say so."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(last_hours=72.0))
    names = [v["key"] for v in spec["visit_schedule"]]
    assert any(k.startswith("p1_ambulatory_d") for k in names)
    assert "p1_confinement" in names
    # Screening and follow-up bracket the whole thing.
    assert names[0] == "screening" and names[-1] == "follow_up"


def test_short_schedule_has_no_ambulatory_visits():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(last_hours=12.0))
    assert not any(v["key"].startswith("p1_ambulatory")
                   for v in spec["visit_schedule"])


# ---------------------------------------------------------------------------
# Label-driven applicability - grounded in the real label, not model recall
# ---------------------------------------------------------------------------


def test_qt_label_switches_on_triplicate_ecgs_and_the_e14_alert():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(), label=QT_LABEL)
    assert "qt_prolongation" in spec["detected_from_label"]
    ecg_fields = {f["name"] for f in
                  next(f for f in spec["forms"] if f["key"] == "eg_ecg")["fields"]}
    assert "replicate_number" in ecg_fields
    alert = next(c for c in spec["edit_checks"] if c["key"] == "ecg_qtcf_alert")
    assert "500" in alert["rule"] and "60" in alert["rule"]


def test_bland_label_does_not_switch_on_hazard_specific_content():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(),
                          label=BLAND_LABEL)
    form_keys = {f["key"] for f in spec["forms"]}
    assert "liver_event_followup" not in form_keys
    assert "injection_site_assessment" not in form_keys
    check_keys = {c["key"] for c in spec["edit_checks"]}
    assert "hys_law_trigger" not in check_keys
    assert "pregnancy_positive_blocks_dosing" not in check_keys
    ecg_fields = {f["name"] for f in
                  next(f for f in spec["forms"] if f["key"] == "eg_ecg")["fields"]}
    assert "replicate_number" not in ecg_fields


def test_hepatotoxicity_label_adds_the_hys_law_form_and_trigger():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(),
                          label=HEPATOTOX_LABEL)
    form = next(f for f in spec["forms"] if f["key"] == "liver_event_followup")
    assert form["detected_from_label"] is True
    assert form["condition_rationale"]
    hys = next(f for f in form["fields"] if f["name"] == "hys_law_case")
    # The three numeric criteria must all be present in the derivation.
    assert "3" in hys["derivation"] and "2" in hys["derivation"]
    assert "cholestasis" in hys["derivation"].lower()
    check = next(c for c in spec["edit_checks"] if c["key"] == "hys_law_trigger")
    assert check["severity"] == "review"


def test_pregnancy_risk_label_hard_blocks_dosing():
    label = {"use_in_specific_populations": (
        "Based on animal data, may cause fetal harm. Advise females of "
        "reproductive potential to use effective contraception.")}
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(), label=label)
    check = next(c for c in spec["edit_checks"]
                 if c["key"] == "pregnancy_positive_blocks_dosing")
    assert check["severity"] == "hard", (
        "dosing a pregnant subject is unrecoverable harm; a query is not a control")
    lb_fields = {f["name"] for f in
                 next(f for f in spec["forms"] if f["key"] == "lb_laboratory")["fields"]}
    assert "pregnancy_result" in lb_fields


def test_controlled_substance_label_adds_witnessed_accountability():
    label = {"drug_abuse_and_dependence": (
        "This product is a Schedule II controlled substance with a high "
        "potential for abuse.")}
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(), label=label)
    fields = {f["name"] for f in
              next(f for f in spec["forms"]
                   if f["key"] == "da_drug_accountability")["fields"]}
    assert {"dea_schedule", "safe_log_signed"} <= fields
    assert "controlled_substance_accountability_balanced" in {
        c["key"] for c in spec["edit_checks"]}


# ---------------------------------------------------------------------------
# Study-type driven content
# ---------------------------------------------------------------------------


def test_food_effect_study_collects_the_standardised_meal_as_data():
    spec = build_crf_spec(design_key="crossover", study_type="food_effect",
                          sampling=_sampling())
    dosing_fields = {f["name"] for f in
                     next(f for f in spec["forms"] if f["key"] == "ex_dosing")["fields"]}
    assert {"meal_start_time", "meal_end_time", "meal_percent_consumed"} <= dosing_fields
    check = next(c for c in spec["edit_checks"] if c["key"] == "meal_timing_food_effect")
    assert "30" in check["rule"]


def test_be_study_does_not_carry_the_food_effect_meal_fields():
    spec = build_crf_spec(design_key="crossover", study_type="BE",
                          sampling=_sampling())
    dosing_fields = {f["name"] for f in
                     next(f for f in spec["forms"] if f["key"] == "ex_dosing")["fields"]}
    assert "meal_percent_consumed" not in dosing_fields


def test_biosimilar_study_always_carries_the_immunogenicity_log():
    """Even when the reference label never uses the word 'immunogenicity'."""
    spec = build_crf_spec(design_key="parallel", study_type="biosimilar_pk",
                          sampling=None, label=BLAND_LABEL)
    assert "is_immunogenicity_log" in {f["key"] for f in spec["forms"]}
    assert "ada_baseline_required" in {c["key"] for c in spec["edit_checks"]}


def test_pd_coprimary_switches_on_the_pd_form():
    ctx = {
        "study_type": "BE",
        "design_decisions": [
            {"key": "pd_endpoint",
             "value": "Glucose infusion rate (euglycemic clamp)"},
        ],
    }
    spec = build_crf_spec(ctx, design_key="crossover", sampling=_sampling())
    assert "pd_assessment" in {f["key"] for f in spec["forms"]}
    pd_form = next(f for f in spec["forms"] if f["key"] == "pd_assessment")
    assert {"elapsed_time_h", "actual_datetime"} <= {
        f["name"] for f in pd_form["fields"]}


@pytest.mark.parametrize("pd_value", ["None (PK only)", "None", "", "n/a"])
def test_pk_only_study_gets_no_pd_form(pd_value):
    """'None (PK only)' is the literal sentinel services/design_decisions emits
    when no validated PD marker responds inside the PK window. Reading it as a
    marker would put a pharmacodynamic collection form into a PK-only protocol.
    """
    ctx = {"design_decisions": [{"key": "pd_endpoint", "value": pd_value}]}
    spec = build_crf_spec(ctx, design_key="crossover", sampling=_sampling())
    assert "pd_coprimary" not in spec["active_conditions"]
    assert "pd_assessment" not in {f["key"] for f in spec["forms"]}


# ---------------------------------------------------------------------------
# Edit-check coverage of the mandated categories
# ---------------------------------------------------------------------------


def test_all_mandated_edit_check_categories_are_present():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    counts = spec["edit_check_counts"]
    for kind in ("range", "cross_form", "missing", "consistency", "uniqueness",
                 "reconciliation"):
        assert counts[kind] > 0, f"no {kind} checks specified"
    assert spec["n_edit_checks"] >= 25


def test_severity_versus_seriousness_confusion_is_flagged_for_review():
    """Confusing AE intensity with regulatory seriousness changes expedited
    reporting obligations; it must be surfaced, but never auto-corrected."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    check = next(c for c in spec["edit_checks"]
                 if c["key"] == "ae_severity_is_not_seriousness")
    assert check["severity"] == "review"
    assert "do not auto-change" in check["action"].lower()


def test_clinically_significant_abnormality_must_become_an_adverse_event():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    check = next(c for c in spec["edit_checks"] if c["key"] == "cs_abnormality_needs_ae")
    assert "ae_adverse_events" in check["forms"]
    assert check["check_type"] == "cross_form"


def test_missing_data_queries_cover_the_required_fields_at_visit_close():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    check = next(c for c in spec["edit_checks"]
                 if c["key"] == "all_required_fields_at_visit_close")
    assert check["check_type"] == "missing"


# ---------------------------------------------------------------------------
# Data Management Plan
# ---------------------------------------------------------------------------


def test_dmp_covers_every_mandated_topic():
    dmp = build_data_management_plan(design_key="crossover", periods=2)
    keys = {s["key"] for s in dmp["sections"]}
    assert {
        "data_flow", "database_build_uat", "external_data", "medical_coding",
        "query_management", "sae_reconciliation", "database_lock",
        "audit_trail_and_part11", "archival",
    } <= keys
    for section in dmp["sections"]:
        assert len(section["content"]) > 200, f"{section['key']} is a stub"
        assert section["responsible"] and section["timing"] and section["reference"]


def test_dmp_names_both_coding_dictionaries_with_a_version_policy():
    dmp = build_data_management_plan(design_key="crossover", periods=2)
    scopes = {d["dictionary"] for d in dmp["coding_dictionaries"]}
    assert any("MedDRA" in s for s in scopes)
    assert any("WHODrug" in s for s in scopes)
    for d in dmp["coding_dictionaries"]:
        assert d["release_cadence"] and d["version_policy"]
        assert "version" in d["version_policy"].lower()


def test_sae_reconciliation_names_both_databases_and_the_join_key():
    dmp = build_data_management_plan(design_key="crossover", periods=2)
    section = next(s for s in dmp["sections"] if s["key"] == "sae_reconciliation")
    text = section["content"].lower()
    assert "safety database" in text and "clinical database" in text
    assert "reference number" in text
    assert "before database lock" in text


def test_database_lock_requires_coding_reconciliation_and_signatures():
    dmp = build_data_management_plan(design_key="crossover", periods=2)
    lock = next(s for s in dmp["sections"] if s["key"] == "database_lock")
    text = lock["content"].lower()
    for requirement in ("queries closed", "coding", "reconciliation",
                        "analysis populations", "unlock"):
        assert requirement in text, f"lock procedure does not mention {requirement}"


def test_audit_trail_section_states_the_part11_requirements():
    dmp = build_data_management_plan(design_key="crossover", periods=2)
    at = next(s for s in dmp["sections"] if s["key"] == "audit_trail_and_part11")
    text = at["content"].lower()
    assert "time-stamped" in text or "time stamped" in text
    assert "cannot be disabled" in text
    assert "old value" in text and "new value" in text
    assert "11.10(e)" in at["reference"]


def test_archival_retention_is_the_longest_applicable_period():
    dmp = build_data_management_plan(design_key="crossover", periods=2)
    assert dmp["retention_years"] == ARCHIVE_RETENTION_YEARS == 25
    archival = next(s for s in dmp["sections"] if s["key"] == "archival")
    assert "25 years" in archival["content"]
    assert "without prior written sponsor" in archival["content"]


def test_external_data_section_names_the_bioanalytical_transfer_and_the_lloq():
    with_lloq = build_data_management_plan(design_key="crossover", periods=2, lloq=0.5)
    section = next(s for s in with_lloq["sections"] if s["key"] == "external_data")
    assert "0.5" in section["content"]
    assert "sample_id" in section["content"]

    without = build_data_management_plan(design_key="crossover", periods=2)
    section2 = next(s for s in without["sections"] if s["key"] == "external_data")
    assert "must be stated in the Data Transfer Agreement" in section2["content"]


def test_missing_lloq_is_flagged_on_the_spec():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    assert "lloq_not_specified" in {f["key"] for f in spec["flags"]}
    spec2 = build_crf_spec(design_key="crossover", sampling=_sampling(), lloq=0.25)
    assert "lloq_not_specified" not in {f["key"] for f in spec2["flags"]}


# ---------------------------------------------------------------------------
# Context extraction
# ---------------------------------------------------------------------------


def test_reads_the_generation_context_shape_the_pipeline_actually_builds():
    ctx = {
        "drug_inn": "metoprolol",
        "study_type": "BE",
        "lloq": 0.5,
        "sample_size": {"design_key": "parallel", "n_enrolled": 48,
                        "t_half_hours": 40.0},
        "label_data": HEPATOTOX_LABEL,
        "population_config": {"dosing": "single"},
    }
    spec = build_crf_spec(ctx)
    assert spec["design_key"] == "parallel"
    assert spec["periods"] == 1
    assert spec["n_enrolled"] == 48
    assert "liver_event_followup" in {f["key"] for f in spec["forms"]}
    assert "lloq_not_specified" not in {f["key"] for f in spec["flags"]}


def test_stored_population_config_shape_also_works():
    cfg = {
        "_sample_size": {"design_key": "crossover", "n_enrolled": 24,
                         "t_half_hours": 12.0},
        "_sampling_schedule": _sampling(),
        "_label": QT_LABEL,
    }
    spec = build_crf_spec(cfg)
    assert spec["periods"] == 2
    assert spec["washout_days"] == pytest.approx(3.5)
    assert spec["pk_sample_log_rows"]
    assert "qt_prolongation" in spec["active_conditions"]


def test_explicit_keyword_overrides_win_over_the_context():
    ctx = {"sample_size": {"design_key": "crossover", "t_half_hours": 12.0}}
    spec = build_crf_spec(ctx, design_key="parallel")
    assert spec["design_key"] == "parallel"
    assert spec["periods"] == 1


def test_no_context_at_all_still_produces_a_usable_specification():
    spec = build_crf_spec()
    assert spec["forms"] and spec["edit_checks"] and spec["visit_schedule"]
    assert spec["summary"]
    assert spec["data_management_plan"]["sections"]


# ---------------------------------------------------------------------------
# Serialisation, directive, and the Windows console constraint
# ---------------------------------------------------------------------------


def test_result_is_json_serialisable_for_population_config():
    """It is persisted inside population_config (JSONB), so it must round-trip."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(),
                          label=HEPATOTOX_LABEL, lloq=0.5)
    restored = json.loads(json.dumps(spec))
    assert restored == spec, "the result must survive a JSONB round-trip unchanged"


def test_dmp_alone_is_json_serialisable():
    dmp = build_data_management_plan(design_key="parallel", periods=1, lloq=1.0)
    assert json.loads(json.dumps(dmp)) == dmp


def test_output_is_pure_ascii():
    """This repo has been bitten by cp1252 console encoding on Windows."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(),
                          label=QT_LABEL)
    json.dumps(spec, ensure_ascii=False).encode("ascii")
    for scope in ("protocol", "crf", "dmp", "all", "sap"):
        crf_dmp_directive(spec, scope=scope).encode("ascii")
    crf_dmp_directive(spec).encode("ascii")


def test_directive_locks_the_specification_for_the_prompt():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(),
                          label=HEPATOTOX_LABEL)
    directive = crf_dmp_directive(spec, scope="all")
    assert "ALREADY DETERMINED" in directive
    # Every active form must appear.
    for form in spec["forms"]:
        assert form["title"] in directive
    # The critical timing fields must be spelled out for the model.
    assert "pc_pk_sample_log.elapsed_time_h" in directive
    assert "ex_dosing.dose_time" in directive
    # The pre-printed timepoints are reproduced verbatim.
    for row in spec["pk_sample_log_rows"]:
        assert f"{row['nominal_time_h']} h" in directive
    # DMP content rides along.
    assert "MedDRA" in directive and "WHODrug" in directive
    assert "Database lock" in directive


def test_directive_scope_splits_crf_from_dmp():
    """No single document needs the whole block, so the integrator must be able
    to inject only the half that document is about."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    everything = crf_dmp_directive(spec, scope="all")
    crf_only = crf_dmp_directive(spec, scope="crf")
    dmp_only = crf_dmp_directive(spec, scope="dmp")

    assert "PK SAMPLE LOG" in crf_only and "PK SAMPLE LOG" not in dmp_only
    assert "DATA MANAGEMENT PLAN sections" in dmp_only
    assert "DATA MANAGEMENT PLAN sections" not in crf_only
    assert len(crf_only) < len(everything)
    assert len(dmp_only) < len(everything)
    # Both halves keep the locked header and the must-disclose tail.
    for part in (crf_only, dmp_only):
        assert "ALREADY DETERMINED" in part


def test_scoping_meaningfully_reduces_the_injected_block():
    """This engine emits far more locked content than the other engines, so
    scoping has to buy a real reduction, not a token one - otherwise a caller
    targeting one section still pays for the whole specification.

    A generous absolute ceiling guards against unbounded growth; the ratios are
    the property actually being tested.
    """
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    everything = len(crf_dmp_directive(spec, scope="all"))
    crf_only = len(crf_dmp_directive(spec, scope="crf"))
    dmp_only = len(crf_dmp_directive(spec, scope="dmp"))
    protocol_only = len(crf_dmp_directive(spec, scope="protocol"))

    assert crf_only < 0.8 * everything
    assert dmp_only < 0.5 * everything
    # The protocol quotes a statement, not a specification.
    assert protocol_only < 0.3 * everything
    # Neither half may quietly grow without bound.
    assert everything < 60000
    assert protocol_only < 12000


# ---------------------------------------------------------------------------
# THE PROTOCOL SCOPE - what the protocol is allowed to quote
# ---------------------------------------------------------------------------
# A protocol is not a case report form specification. The reviewing clinical
# pharmacologist asked for the CRF field-level content to come out of the
# protocol and to live in the separate SAP / CRF-specification document. These
# tests pin that contract from both ends.


def _protocol_directive(**kw):
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(), **kw)
    return spec, crf_dmp_directive(spec, scope="protocol")


def test_protocol_is_the_default_scope():
    """A caller that just passes the result must get the PROTOCOL rendering.

    services/three_stage_pipeline.py calls the directive with no scope at all,
    so the default is what actually reaches a generated protocol.
    """
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    assert crf_dmp_directive(spec) == crf_dmp_directive(spec, scope="protocol")


def test_unknown_scope_falls_back_to_protocol_not_to_everything():
    """A typo must not dump the whole specification into a protocol."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    assert (crf_dmp_directive(spec, scope="crf_dmp")
            == crf_dmp_directive(spec, scope="protocol"))
    assert crf_dmp_directive(spec, scope="") == crf_dmp_directive(spec,
                                                                  scope="protocol")


def test_protocol_scope_omits_every_field_level_specification():
    """The three domain field lists the reviewer called out by name - the dosing
    log (EX), the PK sample collection log (PC) and the adverse events form
    (AE) - must not be rendered into protocol prompt text at all."""
    spec, directive = _protocol_directive(label=HEPATOTOX_LABEL)

    # Section headers that introduce the specification tables.
    for header in ("FIELDS ON THE CRITICAL FORMS", "FORMS (title",
                   "VISIT STRUCTURE", "PROGRAMMED EDIT CHECKS",
                   "PK SAMPLE LOG - one pre-printed row"):
        assert header not in directive, f"{header} leaked into the protocol scope"

    # No qualified field reference from ANY form, critical or not. This is the
    # rendering shape "<form>.<field> | type | units | permitted | required".
    for form in spec["forms"]:
        for field in form["fields"]:
            assert f"{form['key']}.{field['name']}" not in directive, (
                f"{form['key']}.{field['name']} was rendered into the protocol")

    # And none of the permitted-value vocabulary that only a CRF spec carries.
    for value in ("K2EDTA plasma", "Definitely related", "Recovered with sequelae",
                  "Prefilled syringe"):
        assert value not in directive


def test_protocol_scope_omits_the_preprinted_pk_sample_log_rows():
    """One row per timepoint per period is a form, not protocol prose. The
    timepoints themselves are stated once, by the sampling schedule engine."""
    spec, directive = _protocol_directive()
    assert spec["pk_sample_log_rows"], "fixture must actually have rows"
    for row in spec["pk_sample_log_rows"]:
        assert f"[{row['label']}]" not in directive
        assert f"window {row['window']}" not in directive
        assert f"{row['index']}. {row['nominal_time_h']} h" not in directive


def test_protocol_scope_omits_the_edit_check_catalogue():
    spec, directive = _protocol_directive()
    for check in spec["edit_checks"]:
        assert check["rule"] not in directive
        assert check["rationale"] not in directive


def test_protocol_scope_states_the_crf_and_dmp_in_one_short_statement():
    """What SHOULD survive: that data are captured on a CRF and managed under a
    Data Management Plan, plus the conventions a protocol reader needs."""
    spec, directive = _protocol_directive()
    assert "ALREADY DETERMINED" in directive
    assert "electronic case report forms" in directive
    assert "Data Management Plan" in directive
    assert "separate controlled document" in directive
    # The actual-versus-nominal rule is the one CRF fact a protocol must state.
    assert "actual clock time" in directive
    assert "nominal times are used for presentation only" in directive
    # Coding, lock, audit trail and retention are protocol-level statements.
    assert "MedDRA" in directive and "WHODrug" in directive
    assert "database lock" in directive.lower()
    assert "21 CFR Part 11" in directive
    assert f"{ARCHIVE_RETENTION_YEARS} years" in directive


def test_protocol_scope_names_the_excluded_material_explicitly():
    """Omitting the tables is not enough: a model handed the counts and the
    domain list will reconstruct them unless it is told not to."""
    spec, directive = _protocol_directive()
    assert "DO NOT WRITE INTO THE PROTOCOL" in directive
    for domain in ("(EX domain)", "(PC domain)", "(AE domain)"):
        assert domain in directive, f"{domain} exclusion not stated"
    assert "pre-printed PK Sample Log rows" in directive
    assert "Statistical Analysis Plan" in directive
    assert "Critical Fields" in directive


def test_protocol_scope_keeps_the_disclosure_obligations():
    """Trimming what is quoted must not drop what must be disclosed."""
    spec, directive = _protocol_directive(label=HEPATOTOX_LABEL)
    assert spec["must_disclose"] is True
    for flag in spec["flags"]:
        if flag["must_surface_in_protocol"]:
            assert flag["message"] in directive
    for assumption in spec["assumptions"]:
        assert str(assumption["key"]) in directive


def test_protocol_scope_does_not_leak_internal_module_names():
    """The downstream-analysis coverage statement names this product's own
    Python modules; that is engineering provenance, not protocol text."""
    spec, directive = _protocol_directive()
    assert "services/" not in directive
    assert "dataset_ingest" not in directive
    assert "nca.py" not in directive


def test_the_engine_still_produces_the_full_specification_for_the_sap():
    """This is about what the PROTOCOL quotes, not about losing the capability.

    The full specification must remain on the result (the standalone CRF/DMP
    export is built from it) and must still be renderable for the separate
    document.
    """
    spec = build_crf_spec(design_key="crossover", sampling=_sampling(),
                          label=HEPATOTOX_LABEL)
    assert len(spec["pk_sample_log_rows"]) == len(_sampling()["timepoints"])
    assert spec["n_edit_checks"] >= 25
    for key in ("ex_dosing", "pc_pk_sample_log", "ae_adverse_events"):
        form = next(f for f in spec["forms"] if f["key"] == key)
        assert form["n_fields"] >= 8

    sap = crf_dmp_directive(spec, scope="sap")
    assert "pc_pk_sample_log.elapsed_time_h" in sap
    assert "ex_dosing.dose_time" in sap
    for row in spec["pk_sample_log_rows"]:
        assert f"{row['nominal_time_h']} h" in sap
    # "sap" is the same rendering as "crf" under the name of the document that
    # is going to carry it.
    assert sap == crf_dmp_directive(spec, scope="crf")


def test_hard_check_rationale_is_kept_and_soft_check_rationale_is_dropped():
    """The checks that BLOCK a site's entry are the ones the protocol has to
    justify; carrying every rationale would double the block for no gain."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    directive = crf_dmp_directive(spec, scope="crf")
    hard = next(c for c in spec["edit_checks"] if c["key"] == "pk_draw_after_dose")
    soft = next(c for c in spec["edit_checks"] if c["key"] == "vitals_physiological_range")
    assert hard["rationale"] in directive
    assert soft["rule"] in directive
    assert soft["rationale"] not in directive


def test_visit_names_are_not_case_mangled():
    """A single-period study must not produce a lower-cased visit name, and
    'Day -1' must keep its capital D."""
    xo = build_crf_spec(design_key="crossover", sampling=_sampling())
    par = build_crf_spec(design_key="parallel", sampling=_sampling(periods=1))
    for spec in (xo, par):
        for v in spec["visit_schedule"]:
            assert v["name"][0].isupper(), f"visit name not capitalised: {v['name']}"
            assert "day -1" not in v["name"], f"case mangled: {v['name']}"


def test_directive_is_empty_for_empty_input():
    assert crf_dmp_directive({}) == ""
    assert crf_dmp_directive(None) == ""
    assert crf_dmp_directive({"forms": []}) == ""


def test_directive_accepts_a_wrapped_result():
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    assert crf_dmp_directive({"crf": spec}) == crf_dmp_directive(spec)


def test_summary_states_the_actual_versus_nominal_time_rule():
    """The summary is what reaches the protocol narrative; if it does not say
    this, the protocol does not say it either."""
    spec = build_crf_spec(design_key="crossover", sampling=_sampling())
    text = spec["summary"].lower()
    assert "actual clock time" in text
    assert "nominal" in text
    assert "meddra" in text and "whodrug" in text
