"""Tests for the population-matched PK meta-analysis.

The single most important behaviour, pinned first: a healthy-volunteer protocol
must NOT be powered on a patient CV, and vice versa. That was a live defect --
the previous _select_cv_for_powering took max() across every retrieved study
regardless of population -- so these tests exist to stop it coming back.
"""
from __future__ import annotations

import json

from services.meta_analysis import (
    POPULATION_HEALTHY,
    POPULATION_PATIENT,
    POPULATION_SPECIAL,
    POPULATION_UNKNOWN,
    build_meta_analysis,
    classify_population,
    meta_analysis_directive,
)


def _mixed() -> list[dict]:
    """Healthy studies at 25% and 22%; a patient study at 41%."""
    return [
        {"parameter": "cv_intra_cmax", "value": 25.0,
         "population": "healthy adult volunteers", "source_pmid": "111"},
        {"parameter": "Cmax", "value": 120, "unit": "ng/mL",
         "population": "healthy adult volunteers", "source_pmid": "111"},
        {"parameter": "AUC0inf", "value": 800, "unit": "ng*h/mL",
         "population": "healthy adult volunteers", "source_pmid": "111"},
        {"parameter": "cv_intra_auc", "value": 22.0,
         "population": "healthy subjects", "source_pmid": "222"},
        {"parameter": "cv_intra_cmax", "value": 41.0,
         "population": "patients with type 2 diabetes", "source_pmid": "333"},
    ]


# ---------------------------------------------------------------------------
# The defect this module exists to prevent
# ---------------------------------------------------------------------------


def test_healthy_protocol_is_never_powered_on_a_patient_cv():
    r = build_meta_analysis(_mixed(), subject_type="healthy")
    assert r["powering_cv_pct"] == 25.0, "must adopt the highest HEALTHY CV, not the 41% patient CV"
    assert r["powering_cv_study"] == "111"
    # The patient study is still reported -- excluded, not hidden.
    assert r["highest_cv_other_populations_pct"] == 41.0
    assert any(s["study"] == "333" for s in r["studies_excluded_wrong_population"])


def test_patient_protocol_is_never_powered_on_a_healthy_volunteer_cv():
    r = build_meta_analysis(_mixed(), subject_type="patient")
    assert r["powering_cv_pct"] == 41.0
    assert r["powering_cv_study"] == "333"
    assert r["highest_cv_other_populations_pct"] == 25.0


def test_the_two_populations_actually_disagree():
    """If these ever match, the filter has silently stopped working."""
    hv = build_meta_analysis(_mixed(), subject_type="healthy")["powering_cv_pct"]
    pt = build_meta_analysis(_mixed(), subject_type="patient")["powering_cv_pct"]
    assert hv != pt


# ---------------------------------------------------------------------------
# Population classification
# ---------------------------------------------------------------------------


def test_patient_cues_beat_healthy_cues_in_a_mixed_string():
    """'healthy volunteers and patients with hepatic impairment' is not a clean
    healthy-volunteer cohort and must not be counted as one."""
    assert classify_population(
        "healthy volunteers and patients with hepatic impairment"
    ) == POPULATION_PATIENT


def test_special_subgroups_are_not_pooled_into_healthy():
    for text in ("healthy elderly subjects", "paediatric patients", "pregnant women",
                 "healthy CYP2D6 poor metabolizers"):
        assert classify_population(text) == POPULATION_SPECIAL, text


def test_unknown_population_is_not_defaulted_to_healthy():
    """An unclassifiable population must not silently contribute to powering."""
    for text in (None, "", "   ", "study participants"):
        assert classify_population(text) == POPULATION_UNKNOWN
    rows = [{"parameter": "cv_intra_cmax", "value": 60.0,
             "population": "study participants", "source_pmid": "999"}]
    r = build_meta_analysis(rows, subject_type="healthy")
    assert r["powering_cv_pct"] is None, "unknown-population CV must not power the study"


def test_plain_healthy_strings_classify_as_healthy():
    for text in ("healthy adults", "healthy male volunteers", "normal subjects"):
        assert classify_population(text) == POPULATION_HEALTHY


# ---------------------------------------------------------------------------
# Table construction
# ---------------------------------------------------------------------------


def test_rows_are_pivoted_into_one_record_per_study():
    r = build_meta_analysis(_mixed(), subject_type="healthy")
    s111 = next(s for s in r["studies"] if s["study"] == "111")
    assert s111["cmax"] == "120 ng/mL"
    assert s111["auc"] == "800 ng*h/mL"
    assert s111["cv_pct"] == 25.0


def test_missing_values_stay_missing_rather_than_being_invented():
    """A study with only a CV must report no AUC/Cmax, so the appendix can
    print 'not reported' instead of the model filling the cell from recall."""
    r = build_meta_analysis(_mixed(), subject_type="patient")
    s333 = next(s for s in r["studies"] if s["study"] == "333")
    assert s333["auc"] is None and s333["cmax"] is None


def test_within_a_study_the_highest_cv_across_metrics_is_taken():
    rows = [
        {"parameter": "cv_intra_cmax", "value": 18.0, "population": "healthy", "source_pmid": "A"},
        {"parameter": "cv_intra_auc", "value": 31.0, "population": "healthy", "source_pmid": "A"},
    ]
    r = build_meta_analysis(rows, subject_type="healthy")
    assert r["powering_cv_pct"] == 31.0


def test_implausible_cv_is_excluded_from_powering_not_silently_used():
    """A 400% CV is an extraction error; adopting it would balloon N."""
    rows = [
        {"parameter": "cv_intra_cmax", "value": 400.0, "population": "healthy", "source_pmid": "BAD"},
        {"parameter": "cv_intra_cmax", "value": 24.0, "population": "healthy", "source_pmid": "OK"},
    ]
    r = build_meta_analysis(rows, subject_type="healthy")
    assert r["powering_cv_pct"] == 24.0
    bad = next(s for s in r["studies"] if s["study"] == "BAD")
    assert bad["cv_excluded_implausible"], "the discard must be recorded, not hidden"


def test_indication_falls_back_to_population_text_for_patients():
    r = build_meta_analysis(_mixed(), subject_type="patient")
    s333 = next(s for s in r["studies"] if s["study"] == "333")
    assert "diabetes" in (s333["indication"] or "").lower()


def test_literature_titles_are_attached_when_available():
    lit = [{"pmid": "111", "title": "A single-dose BE study", "year": 2019}]
    r = build_meta_analysis(_mixed(), subject_type="healthy", literature=lit)
    s111 = next(s for s in r["studies"] if s["study"] == "111")
    assert s111["title"] == "A single-dose BE study"
    assert s111["year"] == 2019


# ---------------------------------------------------------------------------
# Degradation, serialisation, directive
# ---------------------------------------------------------------------------


def test_no_matching_studies_yields_no_cv_and_says_so():
    rows = [{"parameter": "cv_intra_cmax", "value": 41.0,
             "population": "patients with cancer", "source_pmid": "P1"}]
    r = build_meta_analysis(rows, subject_type="healthy")
    assert r["powering_cv_pct"] is None
    assert "no healthy-volunteer studies" in r["summary"].lower()


def test_empty_input_does_not_crash():
    r = build_meta_analysis([], subject_type="healthy")
    assert r["studies"] == [] and r["powering_cv_pct"] is None
    assert meta_analysis_directive(r) == ""


def test_result_is_json_serialisable_for_population_config():
    r = build_meta_analysis(_mixed(), subject_type="healthy")
    assert json.loads(json.dumps(r))["powering_cv_pct"] == 25.0


def test_directive_marks_non_matching_studies_and_forbids_invention():
    r = build_meta_analysis(_mixed(), subject_type="healthy")
    d = meta_analysis_directive(r)
    assert "ALREADY DETERMINED" in d
    assert "OTHER POPULATION" in d, "excluded studies must be visibly flagged"
    assert "fabrication" in d.lower(), "must forbid filling empty cells from recall"
    assert d.isascii(), "Windows cp1252 console safety"
    for s in r["studies"]:
        assert str(s["study"]) in d
