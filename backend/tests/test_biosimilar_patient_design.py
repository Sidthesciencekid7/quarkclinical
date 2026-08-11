"""The clinical pharmacologist's design rules for a biosimilar PK study in patients.

Twelve requirements, given as two groups. Each has at least one test here that
would FAIL if the rule were removed from the backend, and the non-regression
tests pin the designs the rules must NOT disturb (healthy-volunteer studies,
single-dose studies, small-molecule bioequivalence).

GROUP A - selecting the patient population (services/patient_population.py).
The aim is the cleanest, most sensitive population for evaluating PK:
  A1  the indication must be on the reference product's label
  A2  no prior exposure to the product OR its drug class, ever
  A3  a monotherapy indication is ideal
  A4  otherwise, the indication with the lowest PK variability in prior trials
  A5  recruitability breaks ties, and only ties

GROUP B - endpoints, duration and statistics:
  B1  co-primary = first-dose AND steady-state AUC(0-tau); both must pass;
      multiple doses; minimum doses to steady state from innovator data
  B2  head-to-head immunogenicity + safety, >= 6 months (EMA 12), with the
      primary PK endpoint read EARLIER than the end of study
  B3  the EU reference product is the comparator, always
  B4  efficacy / validated biomarkers are SECONDARY, never powered on
  B5  90% power
  B6  sample size from the literature meta-analysis; alpha handled explicitly
      for the co-primary endpoints
  B7  FDA 351(k) biosimilar guidance governs

NOTE ON B6 AND ALPHA. The requirement was stated as "adjust alpha for co-primary
endpoints". What the backend does, and what these tests pin, is the
statistically correct handling of that requirement, which splits in two:
  * ALPHA is NOT reduced. All co-primary endpoints must pass, which makes the
    decision rule an intersection-union test; its family-wise type I error is
    already bounded by alpha, so splitting alpha would tighten every confidence
    interval for a multiplicity that is already controlled and inflate N for no
    protection. Each endpoint keeps alpha = 0.05 / the 90% CI. The engine states
    this explicitly rather than leaving it looking overlooked.
  * POWER *is* adjusted, and this is the adjustment that co-primary endpoints
    genuinely require: requiring both endpoints to pass makes overall power
    LOWER than per-endpoint power, so per-endpoint power is raised until
    OVERALL power reaches the required 90%.
A Bonferroni alpha split is implemented and tested too, for the union
("any endpoint may win") structure where type I error really does accumulate.
"""
from __future__ import annotations

import pytest

from services.biostatistics import (
    DEFAULT_POWER,
    co_primary_endpoints_for,
    recommend_sample_size,
)
from services.comparative_safety import (
    SAFETY_MONTHS_EU,
    SAFETY_MONTHS_FLOOR,
    build_duration_plan,
    comparative_safety_directive,
    doses_to_steady_state,
    immunogenicity_schedule,
)
from services.guidelines import reference_product_term, regulatory_directive
from services.multiplicity import (
    TEST_STRUCTURE_UNION,
    multiplicity_directive,
    plan_co_primary,
    required_per_endpoint_power,
)
from services.patient_population import (
    patient_population_directive,
    select_patient_population,
)
from services.sampling_schedule import compute_sampling_schedule, sampling_schedule_directive

ADA_LABEL = {
    "indications_usage": (
        "indicated for moderate-to-severe chronic plaque psoriasis, rheumatoid arthritis, "
        "psoriatic arthritis, ankylosing spondylitis, Crohn's disease and ulcerative colitis"
    )
}


def _meta(cv_by_population: dict[str, float]) -> dict:
    studies = [
        {"cv_pct": cv, "population_text": pop, "indication": pop, "title": f"PK in {pop}"}
        for pop, cv in cv_by_population.items()
    ]
    # _observed_cv_for only searches studies_matching (population-matched
    # studies) - see test_a4_uses_studies_matching_not_the_full_wrong_
    # population_list below for the fixture that deliberately puts a study
    # ONLY in "studies" to prove the filter is real.
    return {"studies": studies, "studies_matching": studies}


def _select(drug="adalimumab", label=ADA_LABEL, meta=None):
    return select_patient_population(
        drug_inn=drug, study_type="biosimilar_pk", subject_type="patient",
        label=label, meta_analysis=meta,
    )


# ---------------------------------------------------------------------------
# A1 - the indication must be on the product label
# ---------------------------------------------------------------------------


def test_a1_indication_is_confirmed_against_the_real_label_text():
    r = _select()
    assert r["applicable"] is True
    assert r["recommended"]["on_label"] is True
    assert "INDICATIONS AND USAGE" in r["recommended"]["label_evidence"]


def test_a1_unconfirmed_indication_is_flagged_not_asserted():
    """With no label text, the engine must NOT claim the indication is licensed.
    Asserting an unverified licence is worse than reporting the gap."""
    r = _select(label={})
    assert r["recommended"]["on_label"] is False
    assert r["label_verified"] is False
    assert any("could not be verified" in a.lower() or "not confirmed" in a.lower()
               for a in r["assumptions"] + [r["recommended"]["label_evidence"]])
    directive = patient_population_directive(r)
    assert "NOT confirmed" in directive


def test_a1_indications_absent_from_the_label_rank_below_confirmed_ones():
    """An indication the label does not mention must never outrank one it does,
    even if the unconfirmed one is otherwise more attractive."""
    label = {"indications_usage": "indicated for rheumatoid arthritis only"}
    r = _select(label=label)
    ranked = r["ranked"]
    first_unconfirmed = next(i for i, x in enumerate(ranked) if not x["on_label"])
    last_confirmed = max(i for i, x in enumerate(ranked) if x["on_label"])
    assert last_confirmed < first_unconfirmed
    assert r["recommended"]["indication"] == "Rheumatoid arthritis"


# ---------------------------------------------------------------------------
# A1 - uncurated molecules (not in the 10-molecule knowledge base) must still
# use the REAL retrieved label, not a single uninformative placeholder.
#
# CONFIRMED BUG from adversarial review: the KB covers only 10 molecules;
# every other real biosimilar target (golimumab, certolizumab, filgrastim,
# pegfilgrastim, epoetin alfa, insulin glargine, aflibercept, natalizumab,
# and every US biosimilar-suffixed INN such as "adalimumab-atto") fell
# through to a fixed placeholder string that the retrieved label text played
# no part in - even when that text plainly listed real, checkable
# indications the shared cue library already recognises.
# ---------------------------------------------------------------------------

_GOLIMUMAB_LABEL = {
    "indications_usage": (
        "SIMPONI is indicated for the treatment of adults with moderately to "
        "severely active rheumatoid arthritis, active psoriatic arthritis, "
        "active ankylosing spondylitis and ulcerative colitis."
    )
}


def test_a1_uncurated_molecule_detects_real_indications_from_the_label():
    r = _select(drug="golimumab", label=_GOLIMUMAB_LABEL, meta=None)
    names = {x["indication"] for x in r["ranked"]}
    assert names == {
        "Rheumatoid arthritis", "Psoriatic arthritis",
        "Ankylosing spondylitis", "Ulcerative colitis",
    }
    assert all(x["on_label"] for x in r["ranked"])
    assert "Principal labelled indication" not in r["recommended"]["indication"]


def test_a1_uncurated_molecule_still_applies_monotherapy_ranking():
    """Even without molecule-specific curation, generically-detected
    candidates still carry a (conservative, uncurated) monotherapy field that
    participates in ranking - they are not just an unordered list."""
    r = _select(drug="golimumab", label=_GOLIMUMAB_LABEL, meta=None)
    assert r["recommended"]["monotherapy"] in ("yes", "possible", "no")
    assert "not curated" in r["recommended"]["monotherapy_note"].lower()


def test_a1_uncurated_molecule_with_no_label_text_falls_back_to_the_placeholder():
    """Non-regression: with genuinely nothing to detect from (no label
    retrieved at all), the honest placeholder - not a fabricated guess - is
    still what is returned."""
    r = _select(drug="golimumab", label=None, meta=None)
    assert "Principal labelled indication" in r["recommended"]["indication"]


def test_a1_uncurated_molecule_with_a_biosimilar_suffixed_inn_still_detects_indications():
    """A real US biosimilar-suffixed INN (e.g. adalimumab-atto) is never in
    the KB (which is keyed on the base INN only) and must not be treated
    worse than any other uncurated molecule."""
    r = _select(
        drug="adalimumab-atto",
        label={"indications_usage": "indicated for plaque psoriasis and rheumatoid arthritis"},
        meta=None,
    )
    names = {x["indication"] for x in r["ranked"]}
    assert "Moderate-to-severe chronic plaque psoriasis" in names
    assert "Rheumatoid arthritis" in names


def test_a1_curated_molecules_are_unaffected_by_the_generic_fallback():
    """Non-regression: a molecule that IS in the curated KB must keep using
    its real, curated per-indication data, not the generic fallback."""
    r = _select()  # adalimumab, curated
    assert r["recommended"]["monotherapy"] == "yes"
    assert "not curated" not in r["recommended"]["monotherapy_note"].lower()


def test_a1_crohns_disease_cue_matches_the_unicode_apostrophe_real_labels_use():
    """CONFIRMED BUG from adversarial review: the Crohn's disease cue only
    tolerated an ASCII apostrophe or none, but real DailyMed SPL text
    commonly uses the Unicode right single quote (U+2019) — with the old
    pattern, a genuinely-licensed Crohn's disease indication would score
    on_label=False and be demoted below every confirmed candidate."""
    label = {"indications_usage": "indicated for adult patients with Crohn’s disease"}
    r = _select(drug="adalimumab", label=label)
    crohns = next(x for x in r["ranked"] if x["indication"] == "Crohn's disease")
    assert crohns["on_label"] is True


def test_a1_a_negated_label_sentence_does_not_confirm_the_indication():
    """CONFIRMED BUG from adversarial review: _label_confirms was a bare
    substring/regex presence test with no negation handling — a real
    'Limitations of Use' carve-out ('NOT indicated for ulcerative colitis')
    would flip on_label=True, the opposite of the label's actual meaning."""
    label = {
        "indications_usage": (
            "Indicated for rheumatoid arthritis. Limitations of Use: XYZ is NOT "
            "indicated for ulcerative colitis."
        )
    }
    # A curated molecule (adalimumab) is used so BOTH indications appear in
    # `ranked` regardless of what the label confirms — this isolates the
    # negation-handling behaviour of on_label from the separate (also fixed)
    # generic-candidate-detection behaviour, which would simply omit a
    # negated-only indication rather than mark it not-on-label.
    r = _select(drug="adalimumab", label=label)
    by_name = {x["indication"]: x for x in r["ranked"]}
    assert by_name["Rheumatoid arthritis"]["on_label"] is True
    assert by_name["Ulcerative colitis"]["on_label"] is False


def test_a1_a_genuine_positive_mention_elsewhere_still_confirms_despite_a_negation_nearby():
    """Non-regression: the SAME cue can be negated in one sentence and
    genuinely confirmed in another sentence of the same label (both patterns
    occur in real labels, e.g. a pediatric carve-out alongside adult
    approval) — the negation check is sentence-local, so the confirmed
    sentence must still win rather than the whole document reading as
    negated."""
    label = {
        "indications_usage": (
            "Limitations of Use: not indicated for rheumatoid arthritis in patients "
            "under 18 years of age. Indicated for adults with active rheumatoid "
            "arthritis."
        )
    }
    r = _select(drug="adalimumab", label=label)
    ra = next(x for x in r["ranked"] if x["indication"] == "Rheumatoid arthritis")
    assert ra["on_label"] is True


def test_a1_except_for_phrasing_is_recognized_as_negation():
    """CONFIRMED GAP from adversarial review of the negation fix: 'except for
    X' did not trip the original narrow negation-marker set, so 'Indicated
    for inflammatory bowel disease, except for ulcerative colitis.' would
    still confirm ulcerative colitis as on-label — the opposite of what the
    sentence says."""
    label = {"indications_usage": "Indicated for inflammatory bowel disease, except for ulcerative colitis."}
    r = _select(drug="adalimumab", label=label)
    uc = next(x for x in r["ranked"] if x["indication"] == "Ulcerative colitis")
    assert uc["on_label"] is False


def test_a1_colon_introduced_negated_list_shares_its_negation_across_semicolons():
    """CONFIRMED GAP from adversarial review: 'Not indicated for the
    following: giant cell arteritis; ulcerative colitis; and Behcet
    disease.' is ONE shared negation across every semicolon-separated list
    item, but a bare per-clause split on ';' treated 'ulcerative colitis;'
    as its own unnegated chunk, incorrectly confirming an explicitly
    excluded indication."""
    label = {
        "indications_usage": (
            "Not indicated for the following: giant cell arteritis; ulcerative "
            "colitis; and Behcet disease."
        )
    }
    r = _select(drug="adalimumab", label=label)
    uc = next(x for x in r["ranked"] if x["indication"] == "Ulcerative colitis")
    assert uc["on_label"] is False


def test_a1_unrelated_not_does_not_falsely_negate_a_genuine_indication():
    """CONFIRMED GAP (a NEW false-negative direction) from adversarial review:
    the original negation fix used a bare '\\bnot\\b' catch-all, which fires
    on ordinary label phrasing describing patient history rather than
    negating the indication itself — 'who have NOT responded adequately to
    conventional DMARD therapy' is extremely common real FDA-label wording
    and has nothing to do with negating rheumatoid arthritis as an
    indication. Since rule 1 is a hard gate, this false negative could
    incorrectly report a genuinely on-label indication as unconfirmed."""
    label = {
        "indications_usage": (
            "Indicated for adult patients with moderately to severely active "
            "rheumatoid arthritis who have not responded adequately to "
            "conventional DMARD therapy."
        )
    }
    r = _select(drug="adalimumab", label=label)
    ra = next(x for x in r["ranked"] if x["indication"] == "Rheumatoid arthritis")
    assert ra["on_label"] is True


def test_a1_rule_1_is_a_genuine_gate_not_just_a_soft_sort_preference():
    """CONFIRMED BUG from adversarial review: rule 1 (on-label) is documented
    as a hard GATE, but was implemented only as the first sort key. When a
    label WAS retrieved and checked but ZERO curated candidates matched it,
    the engine still returned a `recommended` indication and the directive
    still said 'ALREADY SELECTED' — reading exactly like a genuinely
    confirmed, locked-in pick rather than a failed check."""
    label = {"indications_usage": "indicated for severe combined immunodeficiency only"}
    r = _select(drug="adalimumab", label=label)
    assert all(not x["on_label"] for x in r["ranked"])
    assert r["no_on_label_candidate"] is True
    assert "NO CANDIDATE INDICATION COULD BE CONFIRMED" in r["summary"].upper()
    directive = patient_population_directive(r)
    assert "NOT YET DETERMINED" in directive
    assert "ALREADY SELECTED" not in directive


def test_a1_rule_1_gate_does_not_trigger_when_the_top_candidate_is_genuinely_on_label():
    """Non-regression: the ordinary confirmed-on-label case must still render
    as a locked selection."""
    r = _select()  # adalimumab, on-label RA/psoriasis/etc via ADA_LABEL
    assert r["no_on_label_candidate"] is False
    directive = patient_population_directive(r)
    assert "ALREADY SELECTED" in directive
    assert "NOT YET DETERMINED" not in directive


# ---------------------------------------------------------------------------
# A2 - never exposed to the product or its class
# ---------------------------------------------------------------------------


def test_a2_naivety_to_product_and_class_are_both_required_exclusions():
    r = _select()
    keys = {rule["key"] for rule in r["eligibility_rules"]}
    assert "naive_to_product" in keys
    assert "naive_to_class" in keys
    by_key = {rule["key"]: rule for rule in r["eligibility_rules"]}
    assert by_key["naive_to_product"]["type"] == "exclusion"
    assert by_key["naive_to_class"]["type"] == "exclusion"
    # The class must be NAMED, not left as a generic phrase.
    assert "TNF" in by_key["naive_to_class"]["criterion"]
    # The reason must be the PK one (ADA alter clearance), not only safety.
    assert "clearance" in by_key["naive_to_product"]["rationale"].lower()


def test_a2_naivety_criteria_reach_the_prompt_as_criteria():
    directive = patient_population_directive(_select())
    assert "[EXCLUSION]" in directive
    assert "prior exposure" in directive.lower()
    assert "worded as criteria" in directive


def test_a2_an_indication_without_a_naive_pool_is_deprioritised():
    """Rituximab's lymphoma indication is late-line — a truly naive cohort is
    not realistically available, so it must rank below the RA indication even
    though RA requires background methotrexate."""
    r = _select(drug="rituximab",
                label={"indications_usage": "indicated for rheumatoid arthritis and non-Hodgkin lymphoma"})
    names = [x["indication"] for x in r["ranked"]]
    assert names.index("Rheumatoid arthritis") < names.index("Follicular / non-Hodgkin lymphoma")


def test_a2_class_naivety_criterion_text_never_doubles_the_word_agent():
    """CONFIRMED BUG from adversarial review: the exclusion-criterion template
    unconditionally appended the word 'agent' after the class name. Two
    curated classes already end in 'agent' ('anti-VEGF agent',
    'erythropoiesis-stimulating agent'), producing 'any other anti-VEGF agent
    agent.'; the uncurated fallback ('biologic agent of the same class')
    produced 'any other biologic agent of the same class agent.' — both
    grammatically broken and, for the fallback, unenforceable as written."""
    for drug, must_not_contain in (
        ("ranibizumab", "agent agent"),
        ("epoetin alfa", "agent agent"),
    ):
        r = _select(drug=drug, label={"indications_usage": "indicated for use"})
        by_key = {rule["key"]: rule for rule in r["eligibility_rules"]}
        criterion = by_key["naive_to_class"]["criterion"]
        assert must_not_contain not in criterion.lower()

    # Uncurated molecule: falls back to the generic phrase, which itself ends
    # in "class" — must not read "... of the same class agent."
    r = _select(drug="some-uncurated-biosimilar", label={"indications_usage": "indicated for use"})
    by_key = {rule["key"]: rule for rule in r["eligibility_rules"]}
    criterion = by_key["naive_to_class"]["criterion"]
    assert "class agent" not in criterion.lower()
    assert criterion.endswith("biologic agent of the same class.")


# ---------------------------------------------------------------------------
# A3 - monotherapy is ideal
# ---------------------------------------------------------------------------


def test_a3_monotherapy_indication_wins_over_combination_therapy():
    r = _select()
    assert r["recommended"]["monotherapy"] == "yes"
    assert r["recommended"]["indication"] == "Moderate-to-severe chronic plaque psoriasis"
    # RA (requires methotrexate) must be ranked below it.
    names = [x["indication"] for x in r["ranked"]]
    assert names.index("Moderate-to-severe chronic plaque psoriasis") < names.index("Rheumatoid arthritis")


def test_a3_monotherapy_beats_a_lower_cv_combination_indication():
    """A3 outranks A4: monotherapy is preferred even when a combination-therapy
    indication happens to show a LOWER measured CV. A background agent is a
    structural confound, not a variance number to be traded off."""
    meta = _meta({"patients with plaque psoriasis": 40.0,
                  "patients with rheumatoid arthritis": 12.0})
    r = _select(meta=meta)
    assert r["recommended"]["monotherapy"] == "yes"
    assert r["recommended"]["indication"] == "Moderate-to-severe chronic plaque psoriasis"


# ---------------------------------------------------------------------------
# A4 - lowest PK variability from prior trials
# ---------------------------------------------------------------------------


def test_a4_lowest_observed_cv_orders_indications_of_equal_monotherapy_status():
    """Infliximab: ankylosing spondylitis and plaque psoriasis are both licensed
    as monotherapy, so A4 decides between them on measured CV."""
    meta = _meta({"patients with ankylosing spondylitis": 15.0,
                  "patients with plaque psoriasis": 45.0})
    r = _select(drug="infliximab",
                label={"indications_usage": "indicated for ankylosing spondylitis, plaque psoriasis, "
                                            "rheumatoid arthritis, Crohn's disease"},
                meta=meta)
    assert r["recommended"]["indication"] == "Ankylosing spondylitis"
    assert r["recommended"]["observed_cv_pct"] == 15.0


def test_a4_uses_the_lowest_not_the_highest_cv_for_a_candidate():
    """Selecting BETWEEN populations takes each candidate's LOWEST demonstrated
    variability — the opposite of meta_analysis.py's max(), which is correct
    once the population is already fixed. Both behaviours are intentional."""
    studies = [
        {"cv_pct": 12.0, "population_text": "plaque psoriasis cohort A", "indication": "", "title": ""},
        {"cv_pct": 48.0, "population_text": "plaque psoriasis cohort B", "indication": "", "title": ""},
    ]
    meta = {"studies": studies, "studies_matching": studies}
    r = _select(meta=meta)
    assert r["recommended"]["observed_cv_pct"] == 12.0


def test_a4_falls_back_to_the_qualitative_tier_without_literature():
    r = _select(meta=None)
    assert r["recommended"]["observed_cv_pct"] is None
    assert r["recommended"]["pk_variability"] in ("low", "moderate", "high", "unknown")


def test_a4_a_single_incidentally_bad_measurement_does_not_flip_the_whole_recommendation():
    """CONFIRMED BUG from adversarial review: the ranking used to encode
    'no measured data' as an infinitely bad placeholder, so ANY candidate
    with a measured CV - however bad - unconditionally outranked every
    candidate without one. A single retrieved study (literature retrieval is
    effectively arbitrary about which indication happens to get a hit) could
    therefore flip the recommendation to the WORST available population."""
    studies = [{"study": "P1", "cv_pct": 70.0,
               "population_text": "patients with ankylosing spondylitis",
               "indication": "Ankylosing spondylitis",
               "title": "PK of adalimumab in ankylosing spondylitis"}]
    meta = {"studies": studies, "studies_matching": studies}
    label = {"indications_usage": "plaque psoriasis rheumatoid arthritis psoriatic "
                                  "arthritis ankylosing spondylitis ulcerative colitis"}
    with_bad_study = _select(label=label, meta=meta)["recommended"]["indication"]
    without_any = _select(label=label, meta=None)["recommended"]["indication"]
    assert with_bad_study == without_any == "Moderate-to-severe chronic plaque psoriasis"


def test_a4_a_genuinely_poor_measurement_can_still_lose_to_an_unmeasured_candidate():
    """The other half of the same fix: an unconditional 'measured always wins'
    rule would be just as wrong as 'unmeasured always wins' - an indication
    whose only literature hit reports severe variability (a value well above
    anything this module's own knowledge base uses) should not be forced
    ahead of an unmeasured, otherwise-preferred candidate at the same
    monotherapy tier."""
    studies = [{"study": "P1", "cv_pct": 95.0,
               "population_text": "patients with ankylosing spondylitis",
               "indication": "Ankylosing spondylitis", "title": ""}]
    meta = {"studies": studies, "studies_matching": studies}
    label = {"indications_usage": "plaque psoriasis and ankylosing spondylitis"}
    r = _select(label=label, meta=meta)
    assert r["recommended"]["indication"] == "Moderate-to-severe chronic plaque psoriasis"


def test_a4_ordinary_measured_cv_still_outranks_an_unmeasured_competitor():
    """Non-regression: an ordinary (not extreme) measured value at the SAME
    monotherapy tier must still be preferable to having no data at all -
    real evidence should not be penalised just for existing."""
    studies = [{"study": "P1", "cv_pct": 20.0,
               "population_text": "patients with ankylosing spondylitis",
               "indication": "Ankylosing spondylitis", "title": ""}]
    meta = {"studies": studies, "studies_matching": studies}
    label = {"indications_usage": "plaque psoriasis and ankylosing spondylitis"}
    r = _select(label=label, meta=meta)
    assert r["recommended"]["indication"] == "Ankylosing spondylitis"
    assert r["recommended"]["observed_cv_pct"] == 20.0


def test_a4_only_population_matched_studies_are_read_never_wrong_population_ones():
    """CONFIRMED BUG from adversarial review: build_meta_analysis splits
    studies into studies_matching (the protocol's own target population) and
    studies_excluded_wrong_population (e.g. healthy-volunteer bridging
    studies) for exactly this reason - meta_analysis.py itself deliberately
    excludes the wrong-population CV from powering. A2's own indication-
    selection engine must apply the same exclusion, or a healthy-volunteer
    CV gets asserted as "the lowest PK variability observed for this
    indication" in a PATIENT population."""
    studies = [{"study": "PMID_HV", "cv_pct": 12.0, "population_class": "healthy",
               "population_text": "healthy adult male volunteers",
               "indication": "Healthy volunteers",
               "title": "PK bridging study of an adalimumab biosimilar in healthy "
                        "subjects supporting the plaque psoriasis indication"}]
    # Deliberately NOT in studies_matching - this is what build_meta_analysis
    # would do for a healthy-volunteer study when the target is "patient".
    meta = {"studies": studies, "studies_matching": []}
    r = _select(meta=meta)
    assert r["recommended"]["observed_cv_pct"] is None
    directive = patient_population_directive(r)
    assert "12.0%" not in directive


# ---------------------------------------------------------------------------
# A5 - recruitability breaks ties only
# ---------------------------------------------------------------------------


def test_a5_recruitment_never_overrides_a_cleaner_population():
    """Etanercept: RA is 'easy' to recruit and psoriasis only 'easy' too, but
    psoriasis is the monotherapy indication — recruitment must not promote RA."""
    r = _select(drug="etanercept",
                label={"indications_usage": "indicated for plaque psoriasis, rheumatoid arthritis, "
                                            "ankylosing spondylitis"})
    assert r["recommended"]["indication"] == "Moderate-to-severe chronic plaque psoriasis"
    assert r["recommended"]["recruitment"] == "easy"


def test_a5_recruitment_is_reported_for_every_candidate():
    r = _select()
    assert all(x["recruitment"] in ("easy", "moderate", "difficult") for x in r["ranked"])


def test_selection_rules_and_rejected_candidates_are_disclosed():
    r = _select()
    assert len(r["selection_rules"]) == 5
    assert r["rejected"], "other labelled indications must be tabulated, not silently dropped"
    directive = patient_population_directive(r)
    assert "DELIBERATELY NOT SELECTED" in directive
    for rejected in r["rejected"]:
        assert rejected["rank_reason"], "every rejected candidate needs a stated reason"


def test_a4_selected_indication_cv_and_powering_cv_can_differ_and_are_both_disclosed():
    """CONFIRMED BUG from adversarial review: the recommended indication's OWN
    lowest observed CV (used to RANK candidates) and the CV the study is
    actually POWERED on (meta_analysis.py's max() over every population-
    matched study, filtered by population CLASS only, never by the specific
    indication) can genuinely differ - a docstring claim that they always
    match was false. Both numbers must reach the prompt, explicitly labelled
    for what they are, rather than only one appearing with no explanation for
    why a reader might see a second, different figure elsewhere in the SAP."""
    from services.meta_analysis import build_meta_analysis

    extracted = [
        {"source_pmid": "11111", "parameter": "cv_inter_auc", "value": 30.0,
         "population": "patients with moderate-to-severe plaque psoriasis"},
        {"source_pmid": "22222", "parameter": "cv_inter_auc", "value": 60.0,
         "population": "patients with active Crohn's disease"},
    ]
    literature = [
        {"pmid": "11111", "title": "Adalimumab PK in plaque psoriasis"},
        {"pmid": "22222", "title": "Adalimumab PK in Crohn's disease"},
    ]
    meta = build_meta_analysis(extracted, subject_type="patient", literature=literature, cv_basis="between")
    r = _select(
        label={"indications_usage": "plaque psoriasis and Crohn's disease"}, meta=meta,
    )
    assert r["recommended"]["indication"] == "Moderate-to-severe chronic plaque psoriasis"
    assert r["recommended"]["observed_cv_pct"] == 30.0
    assert r["powering_cv_pct"] == 60.0  # the higher, Crohn's-disease-derived figure
    assert r["cv_reconciliation_note"]
    directive = patient_population_directive(r)
    assert "30.0" in directive and "not necessarily" in directive.lower()


def test_a4_matching_cvs_produce_no_spurious_reconciliation_note():
    """Non-regression: when the two figures genuinely agree (the common case
    where only one population-matched study exists), no confusing disclosure
    should be manufactured."""
    studies = [{"cv_pct": 22.0, "population_text": "patients with plaque psoriasis",
               "indication": "Psoriasis", "title": ""}]
    meta = {"studies": studies, "studies_matching": studies, "powering_cv_pct": 22.0}
    r = _select(meta=meta)
    assert r["recommended"]["observed_cv_pct"] == 22.0
    assert r["cv_reconciliation_note"] == ""


# ---------------------------------------------------------------------------
# Applicability gates - the population engine must not touch other designs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subject_type,study_type", [
    ("healthy", "biosimilar_pk"),
    ("patient", "BE"),
    ("healthy", "BE"),
    ("patient", "food_effect"),
])
def test_population_engine_is_inert_outside_patient_biosimilar_studies(subject_type, study_type):
    r = select_patient_population(
        drug_inn="adalimumab", study_type=study_type, subject_type=subject_type, label=ADA_LABEL,
    )
    assert r["applicable"] is False
    assert patient_population_directive(r) == ""


# ---------------------------------------------------------------------------
# B1 - first-dose AND steady-state AUC(0-tau) as combined co-primary
# ---------------------------------------------------------------------------


def test_b1_multiple_dose_biosimilar_co_primary_is_first_dose_and_steady_state_auc_tau():
    co, secondary = co_primary_endpoints_for(
        study_type="biosimilar_pk", dosing="multiple", subject_type="patient")
    assert len(co) == 2
    assert "First-dose AUC(0-tau)" in co[0]
    assert "Steady-state AUC(0-tau)" in co[1]
    # Cmax is demoted to secondary — the requirement names AUC as the endpoint.
    assert any("Cmax" in s for s in secondary)


def test_b1_auc_inf_is_never_the_endpoint_in_a_multiple_dose_patient_study():
    """A patient stays on treatment, so the terminal elimination phase is never
    observed and AUC(0-inf) is not estimable. Naming it would specify a quantity
    the study structurally cannot measure."""
    r = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=28.0,
        cv_source="literature", dosing="multiple", subject_type="patient")
    assert "AUC(0-inf)" not in r.endpoint
    assert "AUC0-inf" not in r.endpoint
    assert "AUC(0-tau)" in r.endpoint


def test_b1_sap_static_instructions_are_overridden_for_the_dual_profile_design():
    """CONFIRMED BUG from adversarial review: _SECTION_CONFIGS['statistical_
    analysis_plan']'s static instructions unconditionally describe a single-
    dose Cmax + AUC0-inf NCA recipe (kel-based terminal slope), directly
    contradicting the correct first-dose/steady-state AUC(0-tau) co-primary
    set stated in the SAME locked-facts block for a multiple-dose biosimilar
    study. The locked directive must explicitly override the static text
    rather than leaving two contradictory endpoint descriptions in one
    prompt."""
    import services.ai_service as ai_service

    dual = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=28.0,
        cv_source="literature", dosing="multiple", subject_type="patient").to_dict()
    directive = ai_service._locked_design_directive(dual)
    assert "ENDPOINT OVERRIDE" in directive
    assert "AUC(0-inf)" in directive  # names what NOT to derive
    assert "kel" in directive.lower()

    for kwargs in (
        dict(study_type="BE", drug_inn="ibuprofen", cv_intra_pct=25.0, cv_source="literature"),
        dict(study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=28.0, cv_source="literature"),
    ):
        conventional = recommend_sample_size(**kwargs).to_dict()
        assert "ENDPOINT OVERRIDE" not in ai_service._locked_design_directive(conventional)


def test_b1_single_dose_endpoint_set_is_unchanged():
    """Non-regression: single-dose studies keep the conventional pair."""
    co, secondary = co_primary_endpoints_for(study_type="BE", dosing="single")
    assert co == ["Cmax", "AUC(0-inf)"]
    assert "AUC(0-t)" in secondary


def test_b1_both_endpoints_must_pass():
    r = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=28.0,
        cv_source="literature", dosing="multiple", subject_type="patient")
    assert "ALL must pass" in r.endpoint
    assert r.multiplicity["test_structure"] == "iut"


def test_b1_minimum_doses_to_steady_state_is_computed_from_half_life_and_tau():
    # 5 half-lives, on a half-life inflated to cover slow clearers. Dose 1 is
    # at t=0, so dose n covers only (n-1) complete intervals - the "+1" is
    # what makes elapsed time at dose n (not dose n-1) reach 5 half-lives.
    assert doses_to_steady_state(336.0, 336.0) == 9      # t1/2 14d, q2w
    assert doses_to_steady_state(336.0, 168.0) == 16     # t1/2 14d, weekly
    # Never fewer than three doses, however short the half-life.
    assert doses_to_steady_state(1.0, 24.0) == 3


def test_b1_doses_to_steady_state_actually_reaches_five_half_lives_not_four():
    """Regression guard for a real off-by-one: dose n is given at
    t=(n-1)*tau, so counting doses as ceil(5T/tau) with no +1 means only
    (n-1) intervals - never a full 5 half-lives - have elapsed BY the time
    that dose is given, across any half-life/tau combination."""
    import math
    for t_half, tau in [(12, 24), (168, 336), (336, 672), (24, 24), (336, 336), (720, 168)]:
        n = doses_to_steady_state(t_half, tau)
        elapsed_at_dose_n = (n - 1) * tau
        inflated_t_half = t_half * 1.5
        assert elapsed_at_dose_n >= 5 * inflated_t_half, (t_half, tau, n)


def test_b1_doses_to_steady_state_covers_all_patients_not_the_median_one():
    """The half-life is inflated before the dose count is taken, so the endpoint
    is at steady state in slow clearers too — a median-based count would bias
    their 'steady-state' AUC low."""
    median_based = 5 * 336.0 / 336.0            # = 5 doses on the raw half-life
    assert doses_to_steady_state(336.0, 336.0) > median_based


def test_b1_missing_half_life_reports_the_gap_instead_of_inventing_a_dose_count():
    plan = build_duration_plan(study_type="biosimilar_pk", dosing="multiple",
                               subject_type="patient", tau_hours=336.0)
    assert plan["doses_to_steady_state"] is None
    assert "innovator" in plan["steady_state_rationale"].lower()
    directive = comparative_safety_directive(plan)
    assert "NOT ESTABLISHED" in directive
    assert "do NOT invent a dose number" in directive


def test_b1_steady_state_must_be_confirmed_by_troughs_not_only_predicted():
    plan = build_duration_plan(study_type="biosimilar_pk", dosing="multiple", subject_type="patient",
                               t_half_hours=336.0, tau_hours=336.0)
    assert plan["troughs_to_demonstrate"] >= 3
    assert "trough" in comparative_safety_directive(plan).lower()


# ---------------------------------------------------------------------------
# B1 - the sampling schedule must ACTUALLY BE ABLE TO MEASURE both co-primary
# endpoints. Naming "First-dose AUC(0-tau)" and "Steady-state AUC(0-tau)" as
# co-primary (above) is not enough on its own - found by adversarial review:
# services/sampling_schedule.py built only ONE within-interval profile for a
# multiple-dose design and forbade the generator from adding a second, so a
# named co-primary endpoint had no schedule behind it at all.
# ---------------------------------------------------------------------------


def _dual_schedule(**kw):
    base = dict(t_half_hours=336.0, tmax_hours=96.0, design_key="parallel",
                study_type="biosimilar_pk", dosing="multiple", is_biologic=True,
                tau_hours=336.0, safety_months=12, subject_type="patient")
    base.update(kw)
    return compute_sampling_schedule(**base)


def test_b1_dual_profile_biosimilar_schedules_both_first_dose_and_steady_state():
    r = _dual_schedule()
    assert r["dual_profile_biosimilar"] is True
    assert len(r["timepoints_first_dose"]) > 0
    assert len(r["timepoints_steady_state"]) > 0
    # First dose is genuinely at hour 0; steady state is genuinely offset.
    assert r["timepoints_first_dose"][0]["hours"] == 0.0
    assert r["timepoints_steady_state"][0]["hours"] > 0.0
    # Both profiles cover exactly one dosing interval (0 to tau).
    assert r["timepoints_first_dose"][-1]["hours"] == pytest.approx(336.0, abs=0.5)
    span = r["timepoints_steady_state"][-1]["hours"] - r["timepoints_steady_state"][0]["hours"]
    assert span == pytest.approx(336.0, abs=0.5)


def test_b1_dual_profile_schedule_requires_patient_not_just_multiple_dose():
    """CONFIRMED BUG from adversarial review of fix #7: compute_sampling_
    schedule() had NO subject_type parameter at all and computed
    dual_profile_biosimilar from dosing+study_type only, so a multiple-dose
    HEALTHY VOLUNTEER biosimilar study still got the full first-dose +
    steady-state dual-profile schedule (and the comparative-safety-period-
    linked ADA series, and the doubled blood-volume audit) - contradicting
    services.biostatistics.co_primary_endpoints_for()'s own patient gate for
    the exact same scenario."""
    healthy = _dual_schedule(subject_type="healthy")
    patient = _dual_schedule(subject_type="patient")
    assert healthy["dual_profile_biosimilar"] is False
    assert patient["dual_profile_biosimilar"] is True
    assert healthy["timepoints_first_dose"] == []
    assert healthy["timepoints_steady_state"] == []
    directive = sampling_schedule_directive(healthy)
    assert "first-dose" not in directive.lower()


def test_b1_steady_state_profile_is_offset_to_the_predicted_steady_state_dose():
    r = _dual_schedule()
    expected_offset = (r["steady_state_dose_number"] - 1) * 336.0
    assert r["timepoints_steady_state"][0]["hours"] == pytest.approx(expected_offset)
    # The offset day matches comparative_safety's own steady_state_start_day
    # for the SAME half-life/tau - the two engines must not disagree on when
    # steady state is reached.
    from services.comparative_safety import doses_to_steady_state
    assert r["steady_state_dose_number"] == doses_to_steady_state(336.0, 336.0)


def test_b1_directive_names_both_profiles_and_the_dose_they_are_collected_at():
    r = _dual_schedule()
    d = sampling_schedule_directive(r)
    assert "first-dose" in d.lower()
    assert "steady-state" in d.lower()
    assert str(r["steady_state_dose_number"]) in d


def test_b1_single_dose_biosimilar_is_unaffected_by_dual_profile_logic():
    """Non-regression: a single-dose biosimilar_pk study (the existing NHV
    design) must not acquire a second profile."""
    r = compute_sampling_schedule(
        t_half_hours=336.0, tmax_hours=96.0, design_key="parallel",
        study_type="biosimilar_pk", is_biologic=True,
    )
    assert r["dual_profile_biosimilar"] is False
    assert r["timepoints_first_dose"] == []
    assert r["timepoints_steady_state"] == []


def test_b1_multi_dose_small_molecule_is_unaffected_by_dual_profile_logic():
    """Non-regression: dual-profile is specific to the patient biosimilar
    design (B1), not "any multiple-dose study"."""
    r = compute_sampling_schedule(
        t_half_hours=8.0, tmax_hours=2.0, design_key="crossover", study_type="BE",
        dosing="multiple",
    )
    assert r["dual_profile_biosimilar"] is False


def test_b1_blood_volume_audit_counts_both_profiles():
    """The steady-state profile is genuinely extra blood from the same
    subject and must be counted, not silently dropped or halved: the PK
    volume must be exactly TWO profiles' worth (one within-interval template
    x draw volume x 2), not one."""
    dual = _dual_schedule()
    n_per_profile = len(dual["timepoints_first_dose"])
    draw_ml = dual["blood_volume"]["draw_volume_ml"]
    expected_pk_ml = n_per_profile * draw_ml * 2  # 2 profiles, 1 period (parallel)
    assert dual["blood_volume"]["pk_volume_ml"] == pytest.approx(expected_pk_ml)


def test_b1_ada_schedule_matches_the_comparative_safety_period_not_a_fixed_short_one():
    """THE FIX for a confirmed bug: before this, the sampling engine's own
    fixed ~85-day ADA schedule and comparative_safety's 6/12-month schedule
    were BOTH injected into the same section as independently "already
    determined" facts, ending months apart. Now the sampling engine defers to
    the actual comparative period."""
    r = _dual_schedule(safety_months=12)
    ada_days = [tp["day"] for tp in r["immunogenicity_timepoints"]]
    assert max(ada_days) > 300  # spans close to the full 12 months, not ~85 days
    # Changing the comparative period changes the ADA schedule accordingly.
    r6 = _dual_schedule(safety_months=6)
    ada_days_6 = [tp["day"] for tp in r6["immunogenicity_timepoints"]]
    assert max(ada_days_6) < max(ada_days)


def test_b1_ada_blood_volume_indexing_does_not_break_with_more_than_five_points():
    """Regression guard: the blood-volume audit used to index a fixed 5-point
    ADA tuple by position; a 12-month schedule can have 6+ points."""
    r = _dual_schedule(safety_months=12)
    assert len(r["immunogenicity_timepoints"]) >= 6
    assert r["blood_volume"]["total_ml"] > 0  # did not raise IndexError


def test_b1_steady_state_troughs_are_genuinely_tau_spaced_dosing_occasions():
    """THE FIX for a confirmed bug: troughs used to be labelled 'N day(s)
    before the profile day' regardless of tau, which is not a real dosing day
    for any q2w/q4w biologic regimen. They must now name real, tau-spaced
    dose numbers."""
    r = _dual_schedule()
    troughs = r["steady_state_troughs"]
    assert len(troughs) == 3
    for t in troughs:
        assert "dose" in t["label"].lower()
    # Consecutive troughs are exactly one dosing interval apart.
    hours = [t["hours"] for t in troughs]
    assert hours == sorted(hours)
    for a, b in zip(hours, hours[1:]):
        assert b - a == pytest.approx(336.0)


# ---------------------------------------------------------------------------
# B2 - head-to-head safety / immunogenicity duration
# ---------------------------------------------------------------------------


def _duration(**kw):
    base = dict(study_type="biosimilar_pk", dosing="multiple", subject_type="patient",
                t_half_hours=336.0, tau_hours=336.0)
    base.update(kw)
    return build_duration_plan(**base)


def test_b2_defaults_to_twelve_months_with_the_six_month_floor_stated():
    plan = _duration()
    assert plan["safety_months"] == SAFETY_MONTHS_EU == 12
    assert plan["safety_floor_months"] == SAFETY_MONTHS_FLOOR == 6
    assert "6 months" in plan["safety_basis"]


def test_b2_us_only_programme_may_use_the_six_month_floor():
    plan = _duration(eu_reference_product=False)
    assert plan["safety_months"] == 6
    assert "12" in plan["safety_basis"]


def test_b2_a_shorter_period_than_the_floor_is_flagged():
    """A specified period below the 6-month floor is flagged. With the default
    fixture's half-life/tau, the PK-readout check (see the EXTENDED test right
    below) ALSO fires and extends the period past 3 months regardless - both
    checks are independent and both must be visible in the assumptions."""
    plan = _duration(safety_months=3)
    assert any("SHORTER" in a for a in plan["assumptions"])
    assert plan["safety_months"] >= SAFETY_MONTHS_FLOOR


def test_b2_a_too_short_period_is_extended_to_fit_the_pk_readout_even_when_explicit():
    """Regression guard: an explicitly-requested comparative period that is too
    short even for the PK readout to complete (not just below the regulatory
    floor) must still be extended - honoring the request literally would leave
    the primary PK endpoint reading AFTER the study's own stated end."""
    plan = _duration(safety_months=3)
    assert plan["safety_months"] == 6
    assert any("EXTENDED" in a for a in plan["assumptions"])


def test_b2_primary_pk_endpoint_is_read_before_the_study_ends():
    """The defining consequence of B2: the study does NOT end when PK is read."""
    plan = _duration()
    note = plan["primary_readout_note"]
    assert "BEFORE THE STUDY ENDS" in note
    assert "distinct from the end-of-study" in note
    # Normalised, because the instruction wraps across lines in the directive.
    directive = " ".join(comparative_safety_directive(plan).split())
    assert "do NOT state that the study ends when the PK endpoint is read" in directive


def test_b2_immunogenicity_sampling_spans_the_whole_comparative_period():
    plan = _duration()
    months = plan["immunogenicity_timepoints_months"]
    assert months[0] == 0.0, "an ADA baseline before the first dose is mandatory"
    assert max(months) == plan["safety_months"], "ADA must be sampled at the final visit"
    assert len(months) >= 5
    directive = comparative_safety_directive(plan)
    assert "neutralising" in directive.lower()


@pytest.mark.parametrize("kw", [
    dict(dosing="single"),
    dict(study_type="BE"),
    dict(subject_type="healthy"),
])
def test_b2_duration_engine_is_inert_for_other_designs(kw):
    plan = _duration(**kw)
    assert plan["applicable"] is False
    assert comparative_safety_directive(plan) == ""


def test_b2_healthy_volunteer_multiple_dose_does_not_get_a_patient_immunogenicity_period():
    """CONFIRMED BUG from adversarial review of fix #7: build_duration_plan()
    accepted subject_type but never read it, so a multiple-dose HEALTHY
    VOLUNTEER biosimilar study still got the full 6-12 month head-to-head
    comparative safety/immunogenicity duration plan - output whose own
    primary_readout_note names a first-dose + steady-state co-primary pair
    that a healthy-volunteer study does not have (see
    services.biostatistics.co_primary_endpoints_for)."""
    plan = _duration(subject_type="healthy")
    assert plan["applicable"] is False
    assert "HEALTHY VOLUNTEER" in plan["reason"]
    patient_plan = _duration(subject_type="patient")
    assert patient_plan["applicable"] is True


# ---------------------------------------------------------------------------
# Additional immunogenicity (ADA) timepoints - user-requested extras
# ---------------------------------------------------------------------------
#
# New user-facing input: extra ADA draws on top of the auto-computed
# comparative-immunogenicity schedule. immunogenicity_schedule() is the
# single source of truth both build_duration_plan() (here) and
# services/sampling_schedule.py's own ADA-hours derivation call, so testing
# the merge here also covers sampling_schedule.py's identical behaviour.


def test_immunogenicity_schedule_merges_a_valid_additional_month():
    base = immunogenicity_schedule(12)
    with_extra = immunogenicity_schedule(12, additional_months=[10.5])
    assert 10.5 not in base
    assert 10.5 in with_extra
    # Nothing else in the auto-computed schedule changes.
    assert set(base) < set(with_extra)


def test_immunogenicity_schedule_drops_an_out_of_range_additional_month():
    """A value outside [0, months] would silently extend the comparative
    period itself - dropped rather than extending it incidentally."""
    result = immunogenicity_schedule(12, additional_months=[15.0, -1.0])
    assert 15.0 not in result
    assert -1.0 not in result
    assert result == immunogenicity_schedule(12)


def test_immunogenicity_schedule_ignores_unparseable_additional_values():
    result = immunogenicity_schedule(12, additional_months=[None, "not a number"])  # type: ignore[list-item]
    assert result == immunogenicity_schedule(12)


def test_build_duration_plan_discloses_accepted_and_rejected_additional_months():
    plan = _duration(additional_immunogenicity_months=[9.5, 50.0])
    assert 9.5 in plan["immunogenicity_timepoints_months"]
    assert 50.0 not in plan["immunogenicity_timepoints_months"]
    joined = " ".join(plan["assumptions"])
    assert "9.5" in joined
    assert "50.0" in joined
    directive = comparative_safety_directive(plan)
    # The generic assumptions-rendering loop in comparative_safety_directive
    # must surface both disclosures without any dedicated rendering code.
    assert "9.5" in directive
    assert "50.0" in directive


def test_build_duration_plan_without_additional_months_is_unchanged():
    """Non-regression: omitting the parameter must not alter the plan."""
    plan_default = _duration()
    plan_empty = _duration(additional_immunogenicity_months=[])
    assert plan_default["immunogenicity_timepoints_months"] == plan_empty["immunogenicity_timepoints_months"]
    assert plan_default["assumptions"] == plan_empty["assumptions"]


def test_additional_immunogenicity_months_validated_against_the_post_extension_period_not_the_original():
    """CONFIRMED test-coverage gap from adversarial review: no existing test
    combined a genuinely-EXTENDED comparative period (B2's own readout-
    completion fix) with a user-requested additional immunogenicity
    timepoint through the real two-engine chain routers/protocols.py wires
    together. A q6w-dosed long-half-life biologic (t_half = tau = 1008h)
    pushes the EU default 12-month period out to 14 months; a requested
    month (13) that would be invalid against the ORIGINAL 12-month period
    but valid against the EXTENDED 14-month one must be ACCEPTED, and
    services/sampling_schedule.py's own ADA-hours derivation (fed the
    duration plan's OWN resolved safety_months, exactly as
    routers/protocols.py wires it) must render the SAME final month set -
    the two engines must never disagree, which is this whole module's
    reason for existing (fix #23/CRITICAL, earlier this session)."""
    plan = build_duration_plan(
        study_type="biosimilar_pk", dosing="multiple", subject_type="patient",
        t_half_hours=1008.0, tau_hours=1008.0, eu_reference_product=True,
        additional_immunogenicity_months=[13.0],
    )
    assert plan["safety_months"] > SAFETY_MONTHS_EU  # genuinely extended past 12
    assert any("EXTENDED" in a for a in plan["assumptions"])
    assert 13.0 in plan["immunogenicity_timepoints_months"]  # invalid at 12mo, valid at the extended value

    sched = compute_sampling_schedule(
        1008.0, 240.0, "parallel", "biosimilar_pk", dosing="multiple",
        subject_type="patient", tau_hours=1008.0,
        safety_months=plan["safety_months"],
        additional_immunogenicity_months=[13.0],
    )
    sched_months = sorted({round(tp["hours"] / (30.44 * 24.0), 2) for tp in sched["immunogenicity_timepoints"]})
    plan_months = sorted(round(m, 2) for m in plan["immunogenicity_timepoints_months"])
    assert sched_months == plan_months


# ---------------------------------------------------------------------------
# B3 + B7 - EU reference product, under FDA 351(k), with the bridge
# ---------------------------------------------------------------------------


def test_b3_comparator_is_the_eu_reference_product():
    assert "EU reference medicinal product" in reference_product_term("FDA-BE-2014", "biosimilar_pk")


def test_b3_eu_comparator_on_a_351k_filing_always_carries_the_scientific_bridge():
    """Naming an EU comparator under 351(k) WITHOUT the three-way bridge would be
    a regulatory error, so the two must never be separable."""
    d = regulatory_directive("FDA-BE-2014", "biosimilar_pk")
    assert "EU reference medicinal product" in d
    assert "SCIENTIFIC BRIDGE" in d
    assert "US-licensed reference product" in d
    assert "three-way" in d.lower()


def test_b7_fda_351k_guidance_governs_the_biosimilar_study():
    d = regulatory_directive("FDA-BE-2014", "biosimilar_pk")
    assert "351(k)" in d
    assert "BPCIA" in d
    assert "ANDA" not in d and "505(j)" not in d


def test_b3_small_molecule_comparator_wording_is_untouched():
    d = regulatory_directive("FDA-BE-2014", "BE")
    assert "Reference Listed Drug" in d
    assert "EU reference medicinal product" not in d
    assert "SCIENTIFIC BRIDGE" not in d


# ---------------------------------------------------------------------------
# B4 - efficacy / biomarkers are secondary and never powered on
# ---------------------------------------------------------------------------


def test_b4_efficacy_is_secondary_in_a_multiple_dose_patient_biosimilar_study():
    from services.design_decisions import determine_design_decisions
    decisions = determine_design_decisions(
        drug_inn="rituximab", study_type="biosimilar_pk", design_key="parallel",
        t_half_hours=480.0, dosing="multiple", subject_type="patient")
    pd = next(d for d in decisions if d.key == "pd_endpoint")
    assert "Secondary" in pd.value
    assert "not powered" in pd.value.lower() or "not powered" in pd.rationale.lower()


def test_b4_the_powered_endpoints_remain_the_pk_pair_only():
    r = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="rituximab", cv_intra_pct=28.0,
        cv_source="literature", dosing="multiple", subject_type="patient")
    assert len(r.co_primary_endpoints) == 2
    assert all("AUC(0-tau)" in e for e in r.co_primary_endpoints)


def test_b4_validated_pd_marker_stays_co_primary_for_a_single_dose_study():
    """Non-regression on an EARLIER, still-valid rule: an insulin biosimilar's
    GIR clamp endpoint remains CO-PRIMARY in a single-dose study. B4 narrows
    only the multiple-dose patient design."""
    from services.design_decisions import determine_design_decisions
    decisions = determine_design_decisions(
        drug_inn="insulin glargine", study_type="biosimilar_pk",
        design_key="crossover", t_half_hours=12.0)
    pd = next(d for d in decisions if d.key == "pd_endpoint")
    assert pd.label == "PD co-primary endpoint"
    assert "Glucose infusion rate" in pd.value
    assert "Secondary" not in pd.value


# ---------------------------------------------------------------------------
# B5 - 90% power
# ---------------------------------------------------------------------------


def test_b5_default_power_is_ninety_percent():
    assert DEFAULT_POWER == 0.90


def test_b5_overall_power_is_ninety_percent_for_the_co_primary_design():
    """The number that matters is OVERALL power — the probability BOTH
    co-primary endpoints pass — not the per-endpoint figure."""
    r = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=28.0,
        cv_source="literature", dosing="multiple", subject_type="patient")
    assert r.overall_power == 0.90
    assert r.power > 0.90, "per-endpoint power must be RAISED to hold overall power at 90%"
    assert r.multiplicity["power_adjusted"] is True


def test_b5_single_dose_study_is_still_powered_at_ninety_per_endpoint():
    r = recommend_sample_size(study_type="BE", drug_inn="ibuprofen",
                              cv_intra_pct=25.0, cv_source="literature")
    assert r.power == 0.90
    assert r.multiplicity["power_adjusted"] is False


def test_b5_conventional_study_is_never_described_as_powered_below_ninety():
    """A single-dose bioequivalence study is sized exactly as guidance intends;
    the directive must not headline an independence-based lower bound that would
    read as under-powering."""
    r = recommend_sample_size(study_type="BE", drug_inn="ibuprofen",
                              cv_intra_pct=25.0, cv_source="literature")
    d = multiplicity_directive(r.multiplicity)
    assert "90% per endpoint" in d
    assert "81" not in d
    assert "do NOT describe the study as powered at any lower figure" in d


# ---------------------------------------------------------------------------
# B5 - the power leak found by adversarial review: `power` on the sample-size
# recommendation is the PER-ENDPOINT figure (raised above the requested value
# for a co-primary design), but every OTHER co-primary design also has "more
# than one endpoint" (the conventional Cmax + AUC(0-inf) pair) - so gating any
# consumer on endpoint COUNT alone, instead of the explicit power_adjusted
# flag, mis-headlines the ~81% independence lower bound for a conventional
# study as if it were a real design target. These tests pin the fix at every
# render site: the locked design directive AND the DOCX/PDF export tables.
# ---------------------------------------------------------------------------


def test_overall_power_is_none_not_a_misleading_number_when_not_adjusted():
    """The root-cause fix: rather than relying on every consumer to check
    power_adjusted before reading overall_power, the misleading independence-
    based estimate is never stored under that name at all when it is not a
    genuine target - so a raw-JSON reader (e.g. the un-curated `sample_size`
    context key several sections already receive) cannot misread it either."""
    r = recommend_sample_size(study_type="BE", drug_inn="ibuprofen",
                              cv_intra_pct=25.0, cv_source="literature")
    assert r.overall_power is None
    assert r.achieved_overall_power is None
    assert r.multiplicity["overall_power"] is None


def test_locked_design_directive_headlines_ninety_percent_not_per_endpoint_value():
    import services.ai_service as ai_service

    conventional = recommend_sample_size(
        study_type="BE", drug_inn="ibuprofen", cv_intra_pct=25.0, cv_source="literature",
    ).to_dict()
    assert ai_service._power_line(conventional) == "Power: 90%;"

    adjusted = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=30.0,
        cv_source="literature", dosing="multiple", subject_type="patient",
    ).to_dict()
    line = ai_service._power_line(adjusted)
    assert "Power: 90% (overall" in line
    assert "94.87%" in line


def test_export_power_rows_headline_ninety_percent_for_conventional_studies():
    """Regression guard for the exact reproduction the review found: DOCX/PDF
    'Target power' must read 90%, never the independence lower bound (~81%
    for the standard two-endpoint pair), for every design that was NOT
    genuinely power-adjusted - which includes every conventional single-dose
    study, biosimilar or not."""
    from routers.export import _power_conclusion_pct, _power_rows

    for kwargs in (
        dict(study_type="BE", drug_inn="ibuprofen", cv_intra_pct=25.0, cv_source="literature"),
        dict(study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=30.0, cv_source="literature"),
    ):
        rec = recommend_sample_size(**kwargs).to_dict()
        rows = dict(_power_rows(rec))
        assert rows["Target power"] == "90%"
        assert "81" not in rows["Target power"]
        assert _power_conclusion_pct(rec) > 85.0  # never the ~81% lower bound


def test_export_power_rows_headline_overall_for_the_genuinely_adjusted_design():
    from routers.export import _power_rows

    rec = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=30.0,
        cv_source="literature", dosing="multiple", subject_type="patient",
    ).to_dict()
    rows = dict(_power_rows(rec))
    target_key = next(k for k in rows if k.startswith("Target power"))
    assert rows[target_key] == "90%"
    assert "overall" in target_key.lower()


def test_design_label_reflects_multiple_dosing_for_a_patient_biosimilar_study():
    """Regression guard: the design label used to hardcode 'Single-dose' for
    every design_key, including a multiple-dose biosimilar study - directly
    contradicting the co-primary first-dose/steady-state endpoints stated two
    lines below it in the same locked-facts block."""
    r = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=30.0,
        cv_source="literature", dosing="multiple", subject_type="patient",
    )
    assert r.design.startswith("Multiple-dose")
    assert "Single-dose" not in r.design

    r_single = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=30.0, cv_source="literature",
    )
    assert r_single.design.startswith("Single-dose")


def test_randomization_design_label_also_reflects_multiple_dosing():
    """The SAME bug existed a second time in services/randomization.py's own
    independent copy of the design labels - a randomisation narrative saying
    'Single-dose' for a multiple-dose study would contradict the sample-size
    determination's now-corrected label in the same document."""
    from services.randomization import generate_allocation

    alloc = generate_allocation(design_key="parallel", n_enrolled=20, seed=1, dosing="multiple")
    assert alloc["design"]["label"].startswith("Multiple-dose")

    alloc_single = generate_allocation(design_key="parallel", n_enrolled=20, seed=1)
    assert alloc_single["design"]["label"].startswith("Single-dose")


# ---------------------------------------------------------------------------
# B6 - meta-analysis sample size + explicit co-primary alpha handling
# ---------------------------------------------------------------------------


def test_b6_per_endpoint_power_is_raised_so_overall_power_reaches_target():
    assert required_per_endpoint_power(1, 0.90) == pytest.approx(0.90)
    assert required_per_endpoint_power(2, 0.90) == pytest.approx(0.9487, abs=1e-4)
    assert required_per_endpoint_power(4, 0.90) == pytest.approx(0.9740, abs=1e-4)


def test_b6_naive_per_endpoint_powering_would_have_under_delivered():
    """Documents the defect the adjustment prevents: two endpoints each at 90%
    give only ~81% overall."""
    assert 0.90 ** 2 == pytest.approx(0.81)
    plan = plan_co_primary(endpoints=["A", "B"], overall_power=0.90)
    assert plan.per_endpoint_power > 0.90
    assert plan.overall_power == 0.90


def test_b6_alpha_is_not_split_for_co_primary_endpoints_and_says_why():
    """All-must-pass => intersection-union test => family-wise type I error is
    already bounded, so alpha stays 0.05 and the 90% CI is retained. The reason
    must be stated so it does not read as an oversight."""
    plan = plan_co_primary(endpoints=["First-dose AUC(0-tau)", "Steady-state AUC(0-tau)"])
    assert plan.alpha_per_endpoint == 0.05
    assert plan.alpha_adjusted is False
    assert plan.ci_pct == 90.0
    assert "intersection-union" in plan.alpha_rationale.lower()
    d = multiplicity_directive(plan.to_dict())
    assert "NOT adjusted" in d
    assert "do not leave it looking like multiplicity was overlooked" in d


def test_b6_alpha_is_split_when_the_structure_is_a_union_test():
    """Where ANY endpoint may carry the claim, type I error genuinely
    accumulates and Bonferroni IS applied — the engine handles both structures
    rather than assuming one."""
    plan = plan_co_primary(endpoints=["A", "B"], test_structure=TEST_STRUCTURE_UNION)
    assert plan.alpha_adjusted is True
    assert plan.alpha_per_endpoint == pytest.approx(0.025)
    assert plan.ci_pct == pytest.approx(95.0)


def test_b6_union_power_direction_is_the_opposite_of_iut():
    """CONFIRMED BUG from adversarial review: a union structure (ANY endpoint
    passing is sufficient) needs per-endpoint power LOWERED below the overall
    target, not raised — the IUT formula (overall ** (1/k)) moves the wrong
    way here. Powering each endpoint at the overall target would deliver MORE
    than the target overall power, oversizing the study for no reason."""
    iut = plan_co_primary(endpoints=["A", "B"], overall_power=0.90)
    union = plan_co_primary(endpoints=["A", "B"], overall_power=0.90,
                             test_structure=TEST_STRUCTURE_UNION)
    assert iut.per_endpoint_power > 0.90
    assert union.per_endpoint_power < 0.90
    # Direct check against the closed-form union power identity.
    k_eff = union.effective_independent_endpoints
    achieved = 1.0 - (1.0 - union.per_endpoint_power) ** k_eff
    assert achieved == pytest.approx(0.90, abs=1e-6)


def test_b6_correlation_reduces_the_power_penalty_monotonically():
    independent = required_per_endpoint_power(2, 0.90, correlation=0.0)
    correlated = required_per_endpoint_power(2, 0.90, correlation=0.5)
    perfect = required_per_endpoint_power(2, 0.90, correlation=1.0)
    assert independent > correlated > perfect
    assert perfect == pytest.approx(0.90)


def test_b6_independence_is_the_conservative_default():
    plan = plan_co_primary(endpoints=["A", "B"])
    assert plan.correlation_assumed == 0.0
    assert "conservatively treated as INDEPENDENT" in plan.power_rationale


def test_b6_co_primary_adjustment_increases_the_sample_size():
    kw = dict(study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=30.0,
              cv_source="literature")
    single = recommend_sample_size(**kw)
    multi = recommend_sample_size(**kw, dosing="multiple", subject_type="patient")
    assert multi.n_evaluable > single.n_evaluable


def test_b6_dual_profile_endpoint_set_requires_patient_not_just_multiple_dose():
    """CONFIRMED BUG from adversarial review: subject_type was threaded through
    co_primary_endpoints_for()'s signature but never read in its body, so a
    multiple-dose HEALTHY VOLUNTEER biosimilar study would silently get the
    same first-dose/steady-state co-primary set (and power adjustment) as a
    patient study, even though a healthy-volunteer regimen is time-boxed with
    a genuine washout — the two endpoints are not measured weeks apart under
    different accumulation conditions."""
    co_patient, _ = co_primary_endpoints_for(
        study_type="biosimilar_pk", dosing="multiple", subject_type="patient")
    co_healthy, _ = co_primary_endpoints_for(
        study_type="biosimilar_pk", dosing="multiple", subject_type="healthy")
    assert any("First-dose" in e for e in co_patient)
    assert not any("First-dose" in e for e in co_healthy)

    r_patient = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=28.0,
        cv_source="literature", dosing="multiple", subject_type="patient")
    r_healthy = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=28.0,
        cv_source="literature", dosing="multiple", subject_type="healthy")
    assert r_patient.power_adjusted is True
    assert r_healthy.power_adjusted is False


def test_b6_overall_power_target_reflects_the_cap_not_the_unreachable_request():
    """CONFIRMED BUG from adversarial review: when the per-endpoint power
    needed to reach the requested overall power is capped (_MAX_PER_ENDPOINT_
    POWER), the plan's `overall_power` field kept reporting the original
    (unreachable) request instead of the true achievable ceiling — even
    though `power_adjusted`'s own contract says `overall_power` is what the
    sample size was actually sized to hit."""
    many_endpoints = [f"Endpoint {i}" for i in range(30)]
    plan = plan_co_primary(endpoints=many_endpoints, overall_power=0.90)
    assert plan.capped is True
    assert plan.overall_power < 0.90
    k_eff = plan.effective_independent_endpoints
    assert plan.overall_power == pytest.approx(plan.per_endpoint_power ** k_eff, abs=1e-6)


def test_b6_capped_power_rationale_contrasts_requested_vs_achieved_not_self_referential():
    """CONFIRMED GAP from adversarial review of fix #8: the numeric
    overall_power field was corrected to the achieved ceiling when capped,
    but power_rationale reused the SAME (now-reassigned) variable, producing
    self-referential prose like 'powering each at 86% would deliver only 1%,
    not 86%' instead of contrasting the ORIGINAL 90% request against the
    86% achieved ceiling."""
    many_endpoints = [f"Endpoint {i}" for i in range(30)]
    plan = plan_co_primary(endpoints=many_endpoints, overall_power=0.90)
    assert plan.capped is True
    assert "not 90%" in plan.power_rationale  # the ORIGINAL 90% request, consistently
    assert "ACHIEVED ceiling" in plan.power_rationale
    assert f"{plan.overall_power * 100:.2f}%" in plan.power_rationale  # the true achieved figure


def test_b6_sample_size_rationale_distinguishes_requested_from_achieved_when_capped():
    """CONFIRMED GAP from adversarial review of fix #8: services/biostatistics
    .py's own rationale sentence used `overall_power_target` (the achieved
    ceiling once fix #8 corrected it) for BOTH the 'sized to deliver X%'
    clause AND the 'powering each at X% instead would have delivered only Y%'
    naive comparison — but the naive comparison should contrast against the
    ORIGINALLY-REQUESTED power, not the achieved one, or the sentence claims
    a lower originally-targeted figure delivers a HIGHER naive result, which
    is backwards."""
    r = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=25.0,
        cv_source="literature", dosing="multiple", subject_type="patient", power=0.999,
    )
    assert r.multiplicity.get("capped") is True
    assert r.overall_power == pytest.approx(0.990025, abs=1e-4)
    assert "originally-targeted 100%" in r.rationale
    assert "sized to deliver 99% OVERALL power" in r.rationale


def test_b6_multiplicity_directive_uses_union_language_for_a_union_plan():
    """CONFIRMED GAP from adversarial review of fix #5: multiplicity_directive()
    still emitted IUT-only language ('ALL of which must pass', 'probability
    that every co-primary endpoint passes') even for test_structure=union,
    directly contradicting the union decision-rule line right beside it."""
    plan = plan_co_primary(endpoints=["A", "B"], overall_power=0.90,
                            test_structure=TEST_STRUCTURE_UNION)
    directive = multiplicity_directive(plan.to_dict())
    assert "ANY ONE of which is sufficient" in directive
    assert "ALL of which must pass" not in directive
    assert "at least one co-primary endpoint passes" in directive
    assert "every co-primary endpoint passes" not in directive
    assert "union test (any may pass)" in directive


def test_b6_multiplicity_directive_iut_language_is_unchanged():
    """Non-regression: the default (IUT) case must keep its original wording."""
    plan = plan_co_primary(endpoints=["A", "B"], overall_power=0.90)
    directive = multiplicity_directive(plan.to_dict())
    assert "ALL of which must pass" in directive
    assert "every co-primary endpoint passes" in directive
    assert "intersection-union test (all must pass)" in directive


def test_a2_dead_class_fallback_stays_grammatical_if_ever_reached():
    """CONFIRMED GAP from adversarial review of fix #14: _eligibility_rules()'s
    unreachable-today fallback (`cls = drug_class or "the same class"`) would
    still have produced the broken 'Any prior exposure, at any time, to any
    other the same class.' if a future caller ever passed an empty
    drug_class directly. Import and call the private helper directly with
    drug_class="" to exercise the otherwise-dead branch."""
    from services.patient_population import _eligibility_rules

    rules = _eligibility_rules("somedrug", "")
    by_key = {r["key"]: r for r in rules}
    criterion = by_key["naive_to_class"]["criterion"]
    assert "the same class" not in criterion or "to any other the same class" not in criterion
    assert criterion.endswith("biologic agent of the same class.")


def test_b6_sample_size_still_comes_from_the_literature_meta_analysis():
    """The CV that powers the study must still be the population-matched
    meta-analysis value, not an assumption."""
    r = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="adalimumab", cv_intra_pct=28.0,
        cv_source="literature (highest observed)", dosing="multiple", subject_type="patient")
    assert r.cv_pct == 28.0
    assert r.cv_source.startswith("literature")


# ---------------------------------------------------------------------------
# Wiring - every engine must reach BOTH generation paths
# ---------------------------------------------------------------------------


def test_engines_are_wired_into_the_per_section_path():
    from services.ai_service import _ENGINE_DIRECTIVE_PLAN, _SECTION_CONFIGS
    planned = {k for plan in _ENGINE_DIRECTIVE_PLAN.values() for k, _ in plan}
    for engine in ("patient_population", "comparative_safety", "multiplicity", "trial_precedent"):
        assert engine in planned, f"{engine} missing from _ENGINE_DIRECTIVE_PLAN"
    assert "patient_population" in _SECTION_CONFIGS["population"]["context_keys"]
    assert "trial_precedent" in _SECTION_CONFIGS["population"]["context_keys"]
    assert "comparative_safety" in _SECTION_CONFIGS["safety_monitoring"]["context_keys"]
    assert "multiplicity" in _SECTION_CONFIGS["statistical_analysis_plan"]["context_keys"]


def test_engines_are_wired_into_the_whole_document_path():
    """The two paths keep INDEPENDENT copies of the engine list, and an engine
    present in one but not the other is not suppressed there — its raw dict is
    serialised into the JSON context block instead, losing the locked narration.
    That drift has happened before, so parity is asserted directly."""
    from services.ai_service import _ENGINE_DIRECTIVE_PLAN
    from services.three_stage_pipeline import _ENGINE_DIRECTIVES, _PROMPT_EXCLUDED_ENGINES

    pipeline_keys = {k for k, _ in _ENGINE_DIRECTIVES}
    for engine in ("patient_population", "comparative_safety", "multiplicity", "trial_precedent"):
        assert engine in pipeline_keys, f"{engine} missing from _ENGINE_DIRECTIVES"
    planned = {k for plan in _ENGINE_DIRECTIVE_PLAN.values() for k, _ in plan}
    assert not (planned - pipeline_keys - _PROMPT_EXCLUDED_ENGINES)


def test_new_engines_do_not_leak_raw_json_into_the_prompt():
    """Every engine whose directive is rendered must also have a curated
    projection, or its full raw dict reaches the model alongside the directive."""
    from services.ai_service import _ENGINE_CONTEXT_FIELDS, _ENGINE_CONTEXT_KEYS
    for engine in ("patient_population", "comparative_safety", "multiplicity", "trial_precedent"):
        assert engine in _ENGINE_CONTEXT_KEYS
        assert engine in _ENGINE_CONTEXT_FIELDS
