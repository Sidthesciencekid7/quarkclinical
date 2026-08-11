"""Tests for inclusion/exclusion-criteria precedent grounded in real prior
trials (a clinical pharmacologist's explicit requirement: "inclusion and
exclusion criteria should be based on the prior trials done in that
indication, reference would be the innovator trial protocols and others in
clinicaltrials.gov").

Two layers, tested separately:
  * services/literature_service.py's fetch_clinicaltrials() + its pure
    parsing helpers (_base_inn, _parse_ctgov_study, _ctgov_row_to_record) -
    the RETRIEVAL layer, cached like fetch_pubmed/fetch_dailymed.
  * services/trial_precedent.py's select_trial_precedents() /
    trial_precedent_directive() - the deterministic FILTER/RANK/RENDER layer
    that turns the raw retrieved (unfiltered-by-indication) trial list into
    LOCKED reference material for the ALREADY-SELECTED indication.
"""
from __future__ import annotations

import re

from services.literature_service import (
    LiteratureService,
    _base_inn,
    _parse_ctgov_study,
)
from services.trial_precedent import (
    MAX_PRECEDENT_TRIALS,
    _condition_matches,
    _split_eligibility_text,
    select_trial_precedents,
    trial_precedent_directive,
)

_CTGOV_URL = re.compile(r"^https://clinicaltrials\.gov/api/v2/studies(\?.*)?$")


def _study(nct_id, condition, status="COMPLETED", phase="PHASE3", criteria=None,
           sex="ALL", min_age="18 Years", max_age="", healthy_volunteers=False):
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": f"A study of X in {condition}"},
            "statusModule": {"overallStatus": status},
            "conditionsModule": {"conditions": [condition]},
            "armsInterventionsModule": {"interventions": [{"name": "Adalimumab", "type": "DRUG"}]},
            "designModule": {"phases": [phase] if phase else []},
            "eligibilityModule": {
                "eligibilityCriteria": criteria or "",
                "sex": sex,
                "minimumAge": min_age,
                "maximumAge": max_age,
                "healthyVolunteers": healthy_volunteers,
            },
        }
    }


_RA_CRITERIA = (
    "Inclusion Criteria:\n\n1. Diagnosis of rheumatoid arthritis per ACR criteria\n"
    "2. Age 18 years or older\n\n"
    "Exclusion Criteria:\n\n1. Active or latent tuberculosis\n2. Prior biologic DMARD use"
)


# ---------------------------------------------------------------------------
# services/literature_service.py — pure parsing helpers
# ---------------------------------------------------------------------------


def test_base_inn_strips_us_biosimilar_suffix():
    assert _base_inn("adalimumab-atto") == "adalimumab"
    assert _base_inn("filgrastim-sndz") == "filgrastim"


def test_base_inn_leaves_non_suffixed_inn_unchanged():
    assert _base_inn("metformin") == "metformin"
    assert _base_inn("adalimumab") == "adalimumab"


def test_base_inn_lowercases_and_strips():
    assert _base_inn("  Adalimumab-ATTO  ") == "adalimumab"


def test_parse_ctgov_study_extracts_all_fields():
    rec = _parse_ctgov_study(_study("NCT01111111", "Rheumatoid Arthritis", criteria=_RA_CRITERIA))
    assert rec["nct_id"] == "NCT01111111"
    assert rec["status"] == "COMPLETED"
    assert rec["phase"] == "PHASE3"
    assert rec["conditions"] == ["Rheumatoid Arthritis"]
    assert rec["interventions"] == ["Adalimumab"]
    assert rec["sex"] == "ALL"
    assert rec["minimum_age"] == "18 Years"
    assert rec["healthy_volunteers"] is False
    assert "Exclusion Criteria" in rec["eligibility_criteria"]


def test_parse_ctgov_study_returns_none_without_nct_id():
    assert _parse_ctgov_study({"protocolSection": {}}) is None
    assert _parse_ctgov_study({}) is None


def test_parse_ctgov_study_degrades_gracefully_for_missing_modules():
    """A real study can genuinely omit any module (e.g. an observational
    study has no phase) — every field must degrade to an honest empty value,
    never raise."""
    minimal = {"protocolSection": {"identificationModule": {"nctId": "NCT99999999"}}}
    rec = _parse_ctgov_study(minimal)
    assert rec["nct_id"] == "NCT99999999"
    assert rec["title"] == ""
    assert rec["status"] == ""
    assert rec["phase"] == ""
    assert rec["conditions"] == []
    assert rec["interventions"] == []
    assert rec["healthy_volunteers"] is None
    assert rec["eligibility_criteria"] == ""


# ---------------------------------------------------------------------------
# services/literature_service.py — fetch_clinicaltrials (HTTP + cache)
# ---------------------------------------------------------------------------


async def test_fetch_clinicaltrials_searches_by_base_inn_and_parses_results(seeded, httpx_mock):
    httpx_mock.add_response(
        method="GET", url=_CTGOV_URL,
        json={"studies": [_study("NCT01111111", "Metabolic Disease", criteria=_RA_CRITERIA)]},
    )
    lit_svc = LiteratureService(seeded.db)

    records = await lit_svc.fetch_clinicaltrials("metformin")

    assert len(records) == 1
    assert records[0]["nct_id"] == "NCT01111111"
    request = httpx_mock.get_requests()[0]
    assert "query.intr=metformin" in str(request.url)


async def test_fetch_clinicaltrials_strips_biosimilar_suffix_in_the_query(seeded, httpx_mock):
    httpx_mock.add_response(method="GET", url=_CTGOV_URL, json={"studies": []})
    # metformin is the only seeded drug; add a second one with a
    # biosimilar-suffixed INN to exercise _base_inn() end-to-end.
    from models import Drug
    import uuid
    drug = Drug(id=uuid.uuid4(), inn="adalimumab-atto")
    seeded.db.add(drug)
    await seeded.db.commit()

    lit_svc = LiteratureService(seeded.db)
    await lit_svc.fetch_clinicaltrials("adalimumab-atto")

    request = httpx_mock.get_requests()[0]
    assert "query.intr=adalimumab" in str(request.url)
    assert "atto" not in str(request.url)


async def test_fetch_clinicaltrials_empty_result_returns_empty_list(seeded, httpx_mock):
    httpx_mock.add_response(method="GET", url=_CTGOV_URL, json={"studies": []})
    lit_svc = LiteratureService(seeded.db)
    assert await lit_svc.fetch_clinicaltrials("metformin") == []


async def test_fetch_clinicaltrials_uses_cache_on_second_call(seeded, httpx_mock):
    """Only ONE HTTP response is registered; a second fetch_clinicaltrials
    call for the same drug must hit the 7-day cache, not issue a second
    request (pytest-httpx raises on an unmatched/unexpected request)."""
    httpx_mock.add_response(
        method="GET", url=_CTGOV_URL,
        json={"studies": [_study("NCT02222222", "Metabolic Disease")]},
    )
    lit_svc = LiteratureService(seeded.db)
    first = await lit_svc.fetch_clinicaltrials("metformin")

    lit_svc2 = LiteratureService(seeded.db)
    second = await lit_svc2.fetch_clinicaltrials("metformin")

    assert first == second
    assert second[0]["nct_id"] == "NCT02222222"


async def test_fetch_clinicaltrials_cached_record_round_trips_every_field(seeded, httpx_mock):
    """The cache stores every field beyond nct_id/title as a JSON blob in the
    `abstract` column (mirroring fetch_dailymed's sections convention) — a
    cache-hit read must reconstruct the SAME record, not a partial one."""
    httpx_mock.add_response(
        method="GET", url=_CTGOV_URL,
        json={"studies": [_study(
            "NCT03333333", "Rheumatoid Arthritis", status="TERMINATED", phase="PHASE2",
            criteria=_RA_CRITERIA, sex="FEMALE", min_age="21 Years", max_age="65 Years",
            healthy_volunteers=True,
        )]},
    )
    lit_svc = LiteratureService(seeded.db)
    await lit_svc.fetch_clinicaltrials("metformin")  # populate cache

    lit_svc2 = LiteratureService(seeded.db)
    cached = (await lit_svc2.fetch_clinicaltrials("metformin"))[0]

    assert cached["status"] == "TERMINATED"
    assert cached["phase"] == "PHASE2"
    assert cached["conditions"] == ["Rheumatoid Arthritis"]
    assert cached["sex"] == "FEMALE"
    assert cached["minimum_age"] == "21 Years"
    assert cached["maximum_age"] == "65 Years"
    assert cached["healthy_volunteers"] is True
    assert "Exclusion Criteria" in cached["eligibility_criteria"]


# ---------------------------------------------------------------------------
# services/trial_precedent.py — _condition_matches
# ---------------------------------------------------------------------------


def test_condition_matches_exact_case_insensitive():
    assert _condition_matches("Rheumatoid arthritis", ["Rheumatoid Arthritis"])


def test_condition_matches_when_condition_is_a_core_phrase_of_the_indication():
    """A registry condition ('Psoriasis') is often a short core name inside a
    longer, qualified selected-indication description."""
    assert _condition_matches("Moderate-to-severe chronic plaque psoriasis", ["Psoriasis"])


def test_condition_matches_when_indication_is_a_core_phrase_of_the_condition():
    assert _condition_matches("Urticaria", ["Chronic Spontaneous Urticaria"])


def test_condition_matches_rejects_unrelated_conditions():
    assert not _condition_matches("Rheumatoid arthritis", ["Juvenile Idiopathic Arthritis"])
    assert not _condition_matches("Rheumatoid arthritis", ["Psoriasis", "Osteoporosis"])


def test_condition_matches_false_for_empty_indication_or_conditions():
    assert not _condition_matches("", ["Rheumatoid Arthritis"])
    assert not _condition_matches("Rheumatoid arthritis", [])


# ---------------------------------------------------------------------------
# services/trial_precedent.py — _split_eligibility_text
# ---------------------------------------------------------------------------


def test_split_eligibility_text_separates_on_the_literal_header():
    inc, exc = _split_eligibility_text(_RA_CRITERIA)
    assert "Diagnosis of rheumatoid arthritis" in inc
    assert "Exclusion Criteria" not in inc  # header itself is not duplicated
    assert "Active or latent tuberculosis" in exc


def test_split_eligibility_text_strips_the_inclusion_header():
    inc, _ = _split_eligibility_text(_RA_CRITERIA)
    assert not inc.lower().startswith("inclusion criteria")


def test_split_eligibility_text_handles_missing_exclusion_header_honestly():
    """No 'Exclusion Criteria' header at all — the whole text is returned as
    inclusion_text with an empty exclusion_text, not a guessed split."""
    inc, exc = _split_eligibility_text("Inclusion Criteria:\n\n1. Age 18+")
    assert "Age 18+" in inc
    assert exc == ""


def test_split_eligibility_text_empty_input():
    assert _split_eligibility_text("") == ("", "")


# ---------------------------------------------------------------------------
# services/trial_precedent.py — select_trial_precedents
# ---------------------------------------------------------------------------


def _pp(indication="Rheumatoid arthritis", applicable=True):
    if not applicable:
        return {"applicable": False}
    return {"applicable": True, "recommended": {"indication": indication}}


def test_not_applicable_for_a_healthy_volunteer_or_non_biosimilar_study():
    r = select_trial_precedents(drug_inn="adalimumab", patient_population=_pp(applicable=False), trials=[])
    assert r["applicable"] is False


def test_not_applicable_when_patient_population_gave_no_recommendation():
    r = select_trial_precedents(
        drug_inn="adalimumab",
        patient_population={"applicable": True, "recommended": None},
        trials=[],
    )
    assert r["applicable"] is False


def test_applicable_but_no_precedent_found_when_no_trials_retrieved():
    r = select_trial_precedents(drug_inn="adalimumab", patient_population=_pp(), trials=[])
    assert r["applicable"] is True
    assert r["precedent_found"] is False
    assert "No ClinicalTrials.gov records were retrieved" in r["reason"]


def test_applicable_but_no_precedent_found_when_none_match_the_indication():
    trials = [
        {"nct_id": "NCT01", "title": "t", "status": "COMPLETED", "phase": "PHASE3",
         "conditions": ["Psoriasis"], "eligibility_criteria": ""},
    ]
    r = select_trial_precedents(drug_inn="adalimumab", patient_population=_pp(), trials=trials)
    assert r["applicable"] is True
    assert r["precedent_found"] is False
    assert "none is registered under a condition matching" in r["reason"]


def test_precedent_found_filters_out_irrelevant_trials_by_condition():
    trials = [
        {"nct_id": "NCT_RA", "title": "RA study", "status": "COMPLETED", "phase": "PHASE3",
         "conditions": ["Rheumatoid Arthritis"], "eligibility_criteria": _RA_CRITERIA,
         "sex": "ALL", "minimum_age": "18 Years", "maximum_age": "", "healthy_volunteers": False},
        {"nct_id": "NCT_PSO", "title": "Psoriasis study", "status": "COMPLETED", "phase": "PHASE3",
         "conditions": ["Psoriasis"], "eligibility_criteria": "", "sex": "ALL",
         "minimum_age": "", "maximum_age": "", "healthy_volunteers": None},
    ]
    r = select_trial_precedents(drug_inn="adalimumab", patient_population=_pp(), trials=trials)
    assert r["applicable"] is True
    assert r["precedent_found"] is True
    assert r["n_trials_relevant"] == 1
    assert [p["nct_id"] for p in r["precedent_trials"]] == ["NCT_RA"]


def test_precedent_trials_ranked_completed_before_terminated():
    trials = [
        {"nct_id": "NCT_TERM", "title": "t", "status": "TERMINATED", "phase": "PHASE2",
         "conditions": ["Rheumatoid Arthritis"], "eligibility_criteria": _RA_CRITERIA,
         "sex": "ALL", "minimum_age": "", "maximum_age": "", "healthy_volunteers": None},
        {"nct_id": "NCT_COMP", "title": "t", "status": "COMPLETED", "phase": "PHASE3",
         "conditions": ["Rheumatoid Arthritis"], "eligibility_criteria": _RA_CRITERIA,
         "sex": "ALL", "minimum_age": "", "maximum_age": "", "healthy_volunteers": None},
    ]
    r = select_trial_precedents(drug_inn="adalimumab", patient_population=_pp(), trials=trials)
    assert [p["nct_id"] for p in r["precedent_trials"]] == ["NCT_COMP", "NCT_TERM"]


def test_precedent_trials_capped_at_max_and_assumption_recorded():
    trials = [
        {"nct_id": f"NCT{i:08d}", "title": "t", "status": "COMPLETED", "phase": "PHASE3",
         "conditions": ["Rheumatoid Arthritis"], "eligibility_criteria": "",
         "sex": "ALL", "minimum_age": "", "maximum_age": "", "healthy_volunteers": None}
        for i in range(MAX_PRECEDENT_TRIALS + 3)
    ]
    r = select_trial_precedents(drug_inn="adalimumab", patient_population=_pp(), trials=trials)
    assert len(r["precedent_trials"]) == MAX_PRECEDENT_TRIALS
    assert any("strongest precedents" in a for a in r["assumptions"])


def test_long_eligibility_text_is_bounded_and_flagged_truncated():
    long_criteria = "Inclusion Criteria:\n\n" + ("A very long criterion sentence. " * 200)
    trials = [
        {"nct_id": "NCT_LONG", "title": "t", "status": "COMPLETED", "phase": "PHASE3",
         "conditions": ["Rheumatoid Arthritis"], "eligibility_criteria": long_criteria,
         "sex": "ALL", "minimum_age": "", "maximum_age": "", "healthy_volunteers": None},
    ]
    r = select_trial_precedents(drug_inn="adalimumab", patient_population=_pp(), trials=trials)
    precedent = r["precedent_trials"][0]
    assert precedent["truncated"] is True
    assert len(precedent["inclusion_text"]) < len(long_criteria)


# ---------------------------------------------------------------------------
# services/trial_precedent.py — trial_precedent_directive
# ---------------------------------------------------------------------------


def test_directive_empty_when_not_applicable():
    assert trial_precedent_directive({"applicable": False}) == ""
    assert trial_precedent_directive(None) == ""


def test_directive_states_not_found_honestly():
    r = select_trial_precedents(drug_inn="adalimumab", patient_population=_pp(), trials=[])
    d = trial_precedent_directive(r)
    assert "NOT FOUND" in d
    assert "do NOT imply this document reviewed a real prior trial" in d


def test_directive_cites_nct_numbers_and_real_criteria_text_when_found():
    trials = [
        {"nct_id": "NCT01111111", "title": "RA study", "status": "COMPLETED", "phase": "PHASE3",
         "conditions": ["Rheumatoid Arthritis"], "eligibility_criteria": _RA_CRITERIA,
         "sex": "ALL", "minimum_age": "18 Years", "maximum_age": "", "healthy_volunteers": False},
    ]
    r = select_trial_precedents(drug_inn="adalimumab", patient_population=_pp(), trials=trials)
    d = trial_precedent_directive(r)
    assert "NCT01111111" in d
    assert "REAL PRIOR TRIALS" in d
    assert "Diagnosis of rheumatoid arthritis" in d
    assert "Active or latent tuberculosis" in d
    # Normalise line-wrap whitespace before checking the instruction phrase,
    # since the directive wraps prose across list entries.
    assert "cite the nct number" in " ".join(d.lower().split())


def test_directive_never_claims_the_full_protocol_was_read():
    trials = [
        {"nct_id": "NCT01111111", "title": "RA study", "status": "COMPLETED", "phase": "PHASE3",
         "conditions": ["Rheumatoid Arthritis"], "eligibility_criteria": _RA_CRITERIA,
         "sex": "ALL", "minimum_age": "", "maximum_age": "", "healthy_volunteers": None},
    ]
    r = select_trial_precedents(drug_inn="adalimumab", patient_population=_pp(), trials=trials)
    d = trial_precedent_directive(r)
    assert "Do NOT claim to have read the full protocol" in d
