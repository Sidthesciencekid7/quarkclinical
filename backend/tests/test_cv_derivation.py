"""Deriving CV% from a reported mean +/- SD when no percentage is stated, and
keeping the result honestly labelled as a BETWEEN-subject quantity.

The bug this closes (part 1, extraction): extract_pk_parameters() only ever
recorded a CV row when a source stated one AS A PERCENTAGE. That is not how a
biologic label or a first-in-human paper usually reports variability -
"mean (+/- SD) Cmax was X +/- Y" is the norm there, not the exception - so a
genuinely retrievable, two-number-division-away CV was silently treated as
absent, and the sample size fell back to an "assumed" CV for a drug whose own
literature answered the question. Confirmed concretely against guselkumab: the
FDA label (DailyMed) states "mean (+/- SD) maximum serum concentration of
8.09 +/- 3.68 mcg/mL" for a single 100 mg SC dose in healthy adult subjects -
CV% = 3.68/8.09*100 = 45.5%, computable but never computed.

derive_cv_from_mean_sd() is the deterministic fix for part 1: Python performs
the division exactly once, the model is never asked to (extract_pk_parameters's
STRICT RULES tell it to transcribe mean+sd, never to calculate a percentage
itself), and the result is clearly flagged (derived_from_mean_sd) so every
consumer - the meta-analysis directive, the SAP's cv_table rendering - can
disclose "derived from mean/SD" rather than presenting it as a value the
source printed outright.

The bug this closes (part 2, statistical correctness - found in senior code
review after part 1 shipped): a mean +/- SD reported across a cohort of
subjects measures how much those subjects differ from EACH OTHER - a
BETWEEN-subject (inter-individual) quantity. It can never measure how much a
single subject differs from themselves on repeat dosing - a WITHIN-subject
(intra-individual) quantity, which is what a crossover or replicate BE design
is powered on and can only be measured by dosing the SAME subjects twice.
Part 1 shipped labelling the derived value cv_intra_cmax/cv_intra_auc and let
it flow into powering completely ungated - correct by luck for a biosimilar
parallel design (which is the only design that consumes its resolved CV
directly, with no within/between conversion), but silently ~2x oversized a
small-molecule crossover study and mis-triggered replicate/RSABE design
selection whenever the inflated "within-subject" number crossed
HVD_CV_THRESHOLD. Part 2 renames the derived value to cv_inter_cmax/
cv_inter_auc and gates build_meta_analysis() on a `cv_basis` the caller states
up front: a value of the wrong basis is tabulated (so the appendix can
disclose it was found and explain why it was not used) but never contributes
to the adopted powering CV - the exact "tabulate, disclose, do not adopt"
treatment this module already gives a CV from the wrong population.
"""
from __future__ import annotations

from services.meta_analysis import build_meta_analysis, derive_cv_from_mean_sd


# ---------------------------------------------------------------------------
# derive_cv_from_mean_sd — unit tests
# ---------------------------------------------------------------------------


def _cmax(pmid: str, value: float, sd: float | None = None, population: str = "healthy adults",
          confidence: float | None = 1.0) -> dict:
    row = {"parameter": "Cmax", "value": value, "unit": "mcg/mL",
           "population": population, "source_pmid": pmid, "confidence": confidence}
    if sd is not None:
        row["sd"] = sd
    return row


def test_real_guselkumab_label_numbers_derive_the_expected_cv():
    """The exact motivating case: FDA label mean (+/- SD) Cmax for a single
    100 mg SC dose of guselkumab in healthy adult subjects. The derived
    parameter must be named cv_inter_cmax (between-subject) - a mean/SD pair
    across a cohort cannot supply a within-subject figure."""
    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    out = derive_cv_from_mean_sd(extracted)

    derived = [p for p in out if p.get("derived_from_mean_sd")]
    assert len(derived) == 1
    row = derived[0]
    assert row["parameter"] == "cv_inter_cmax"
    assert row["value"] == 45.5  # round(3.68 / 8.09 * 100, 1)
    assert row["unit"] == "%"
    assert row["source_pmid"] == "DAILYMED"
    assert row["population"] == "healthy adult subjects"
    # The original mean row must still be present, unmodified.
    assert any(p["parameter"] == "Cmax" and p["value"] == 8.09 for p in out)


def test_auc_mean_sd_derives_cv_inter_auc():
    extracted = [
        {"parameter": "AUC0inf", "value": 100.0, "sd": 30.0, "unit": "mcg*d/mL",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    out = derive_cv_from_mean_sd(extracted)
    derived = [p for p in out if p.get("derived_from_mean_sd")]
    assert len(derived) == 1
    assert derived[0]["parameter"] == "cv_inter_auc"
    assert derived[0]["value"] == 30.0


def test_auc0t_also_triggers_derivation():
    extracted = [
        {"parameter": "AUC0t", "value": 80.0, "sd": 20.0, "unit": "mcg*d/mL",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    derived = [p for p in derive_cv_from_mean_sd(extracted) if p.get("derived_from_mean_sd")]
    assert len(derived) == 1
    assert derived[0]["parameter"] == "cv_inter_auc"
    assert derived[0]["value"] == 25.0


def test_no_sd_means_no_derivation_and_list_is_the_same_object():
    """No SD present -> nothing to derive. Returns the SAME list object (not a
    copy) so a caller with no mean/SD pairs at all pays no allocation cost."""
    extracted = [_cmax("P1", 8.09)]
    out = derive_cv_from_mean_sd(extracted)
    assert out is extracted


def test_zero_or_negative_sd_is_not_derived():
    for bad_sd in (0.0, -1.0):
        extracted = [_cmax("P1", 8.09, sd=bad_sd)]
        out = derive_cv_from_mean_sd(extracted)
        assert not any(p.get("derived_from_mean_sd") for p in out)


def test_zero_or_negative_mean_is_not_derived():
    for bad_mean in (0.0, -5.0):
        extracted = [_cmax("P1", bad_mean, sd=1.0)]
        out = derive_cv_from_mean_sd(extracted)
        assert not any(p.get("derived_from_mean_sd") for p in out)


def test_explicit_cv_intra_cmax_wins_no_derivation():
    """A source that gives BOTH a mean+SD row and an explicit within-subject
    cv_intra_cmax for the same PMID must not get a redundant/conflicting
    derived row - the explicit value always wins, regardless of basis."""
    extracted = [
        _cmax("P1", 8.09, sd=3.68),
        {"parameter": "cv_intra_cmax", "value": 22.0, "unit": "%",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    out = derive_cv_from_mean_sd(extracted)
    assert not any(p.get("derived_from_mean_sd") for p in out)
    # The explicit value is untouched and still present.
    assert any(p["parameter"] == "cv_intra_cmax" and p["value"] == 22.0 for p in out)


def test_explicit_cv_inter_cmax_also_wins_no_derivation():
    """Symmetric with the within-subject case: an explicit BETWEEN-subject CV
    for the same PMID/metric must also block a redundant derived row."""
    extracted = [
        _cmax("P1", 8.09, sd=3.68),
        {"parameter": "cv_inter_cmax", "value": 40.0, "unit": "%",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    out = derive_cv_from_mean_sd(extracted)
    assert not any(p.get("derived_from_mean_sd") for p in out)
    assert any(p["parameter"] == "cv_inter_cmax" and p["value"] == 40.0 for p in out)


def test_generic_cv_intra_for_a_pmid_blocks_derivation_for_both_metrics():
    """A generic (metric-unspecified) explicit CV for a PMID is conservatively
    treated as covering BOTH Cmax and AUC for that PMID, to avoid double
    counting the same reported variability under two different labels."""
    extracted = [
        _cmax("P1", 8.09, sd=3.68),
        {"parameter": "AUC0inf", "value": 100.0, "sd": 30.0, "unit": "mcg*d/mL",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
        {"parameter": "cv_intra", "value": 28.0, "unit": "%",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    out = derive_cv_from_mean_sd(extracted)
    assert not any(p.get("derived_from_mean_sd") for p in out)


def test_explicit_cv_for_one_metric_does_not_block_the_other():
    """An explicit AUC CV must not suppress deriving a Cmax CV for the SAME
    pmid when Cmax only has mean+SD - the two metrics are independent."""
    extracted = [
        _cmax("P1", 8.09, sd=3.68),
        {"parameter": "cv_intra_auc", "value": 30.0, "unit": "%",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    out = derive_cv_from_mean_sd(extracted)
    derived = [p for p in out if p.get("derived_from_mean_sd")]
    assert len(derived) == 1
    assert derived[0]["parameter"] == "cv_inter_cmax"


def test_explicit_cv_for_one_pmid_does_not_block_derivation_for_another():
    extracted = [
        _cmax("P1", 8.09, sd=3.68),
        {"parameter": "cv_intra_cmax", "value": 22.0, "unit": "%",
         "population": "healthy adults", "source_pmid": "P2", "confidence": 1.0},
    ]
    derived = [p for p in derive_cv_from_mean_sd(extracted) if p.get("derived_from_mean_sd")]
    assert len(derived) == 1
    assert derived[0]["source_pmid"] == "P1"


def test_multiple_dose_cohorts_in_one_pmid_each_derive_their_own_row():
    """A first-in-human paper reporting mean+SD for several dose cohorts
    (e.g. 10 mg and 300 mg) must not be collapsed into one derived row -
    build_meta_analysis's own max() picks the conservative one downstream."""
    extracted = [
        _cmax("P1", 4.0, sd=1.0, population="healthy adults 10 mg"),
        _cmax("P1", 8.09, sd=3.68, population="healthy adults 300 mg"),
    ]
    derived = [p for p in derive_cv_from_mean_sd(extracted) if p.get("derived_from_mean_sd")]
    assert len(derived) == 2
    values = sorted(d["value"] for d in derived)
    assert values == [25.0, 45.5]


def test_confidence_is_capped_below_a_directly_stated_value():
    extracted = [_cmax("P1", 8.09, sd=3.68, confidence=1.0)]
    derived = [p for p in derive_cv_from_mean_sd(extracted) if p.get("derived_from_mean_sd")]
    assert derived[0]["confidence"] <= 0.85


def test_confidence_missing_still_produces_a_capped_value():
    extracted = [_cmax("P1", 8.09, sd=3.68, confidence=None)]
    derived = [p for p in derive_cv_from_mean_sd(extracted) if p.get("derived_from_mean_sd")]
    assert derived[0]["confidence"] == 0.85


def test_population_field_is_carried_through_verbatim():
    extracted = [_cmax("P1", 8.09, sd=3.68, population="healthy Japanese male subjects")]
    derived = [p for p in derive_cv_from_mean_sd(extracted) if p.get("derived_from_mean_sd")]
    assert derived[0]["population"] == "healthy Japanese male subjects"


def test_unrelated_parameters_are_never_touched():
    extracted = [
        {"parameter": "t_half", "value": 336.0, "sd": 48.0, "unit": "h",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    out = derive_cv_from_mean_sd(extracted)
    assert not any(p.get("derived_from_mean_sd") for p in out)
    assert len(out) == 1  # t_half's sd is simply irrelevant, not an error


# ---------------------------------------------------------------------------
# Integration with build_meta_analysis — cv_basis gating
# ---------------------------------------------------------------------------


def test_mean_sd_only_literature_finds_nothing_without_derivation():
    """Baseline: without derive_cv_from_mean_sd, mean/SD-only literature
    contributes no CV at all, regardless of basis - the exact "no CV found"
    the derivation feature addresses."""
    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    result = build_meta_analysis(extracted, subject_type="healthy")
    assert result["powering_cv_pct"] is None


def test_derived_between_subject_cv_does_not_power_a_within_subject_design():
    """THE CRITICAL FIX. cv_basis="within" is what every non-biosimilar
    (crossover/replicate-eligible) design needs. A derived value is
    structurally a between-subject quantity (see module docstring) and must
    NOT be adopted to power a within-subject design - it is tabulated for
    disclosure only, and the sample size correctly falls back to none/assumed
    rather than silently using an inflated figure."""
    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    result = build_meta_analysis(
        derive_cv_from_mean_sd(extracted), subject_type="healthy", cv_basis="within",
    )
    assert result["powering_cv_pct"] is None
    assert result["cv_basis"] == "within"
    # But it is NOT silently dropped - it is disclosed as found-but-wrong-basis.
    assert result["highest_cv_wrong_basis_pct"] == 45.5
    study = result["studies"][0]
    assert study["cv_pct"] is None
    assert study["cv_wrong_basis_pct"] == 45.5
    assert study["cv_wrong_basis_metrics"] == [
        {"metric": "cv_inter_cmax", "cv_pct": 45.5, "derived_from_mean_sd": True}
    ]


def test_derived_between_subject_cv_powers_a_between_subject_design():
    """The positive case: cv_basis="between" is what a biosimilar parallel
    design needs (services/biostatistics.py feeds it straight through with no
    within/between conversion), and a mean/SD-derived value IS the right
    quantity for that design - it is adopted."""
    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    result = build_meta_analysis(
        derive_cv_from_mean_sd(extracted), subject_type="healthy", cv_basis="between",
    )
    assert result["powering_cv_pct"] == 45.5
    assert result["powering_cv_derived_from_mean_sd"] is True
    assert result["cv_basis"] == "between"


def test_explicit_cv_literature_is_unaffected_by_wrapping_with_derive():
    """Non-regression: wrapping extracted with derive_cv_from_mean_sd must not
    change the result at all when the source already gives an explicit,
    correctly-basis-matched within-subject CV%."""
    extracted = [
        {"parameter": "cv_intra_cmax", "value": 24.5, "unit": "%",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    plain = build_meta_analysis(extracted, subject_type="healthy")
    wrapped = build_meta_analysis(derive_cv_from_mean_sd(extracted), subject_type="healthy")
    assert plain["powering_cv_pct"] == wrapped["powering_cv_pct"] == 24.5
    assert wrapped["powering_cv_derived_from_mean_sd"] is False


def test_explicit_within_subject_cv_wins_over_a_higher_derived_between_subject_one():
    """A within-subject design must prefer an explicit, correctly-basis-matched
    CV even when a numerically HIGHER derived (wrong-basis) value exists
    elsewhere in the literature - basis correctness comes before magnitude."""
    extracted = [
        {"parameter": "cv_intra_cmax", "value": 24.5, "unit": "%",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
        _cmax("P2", 8.09, sd=3.68, population="healthy adults"),  # derives cv_inter_cmax=45.5
    ]
    result = build_meta_analysis(
        derive_cv_from_mean_sd(extracted), subject_type="healthy", cv_basis="within",
    )
    assert result["powering_cv_pct"] == 24.5
    assert result["powering_cv_study"] == "P1"
    assert result["powering_cv_derived_from_mean_sd"] is False
    assert result["highest_cv_wrong_basis_pct"] == 45.5


def test_same_mixed_literature_powers_a_between_subject_design_on_the_derived_value():
    """The SAME literature as above, but for a between-subject design: now the
    explicit within-subject P1 value is the wrong basis and is excluded, and
    the derived P2 value is the only eligible (and adopted) candidate."""
    extracted = [
        {"parameter": "cv_intra_cmax", "value": 24.5, "unit": "%",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
        _cmax("P2", 8.09, sd=3.68, population="healthy adults"),
    ]
    result = build_meta_analysis(
        derive_cv_from_mean_sd(extracted), subject_type="healthy", cv_basis="between",
    )
    assert result["powering_cv_pct"] == 45.5
    assert result["powering_cv_study"] == "P2"
    assert result["powering_cv_derived_from_mean_sd"] is True
    assert result["highest_cv_wrong_basis_pct"] == 24.5


def test_implausible_derived_cv_is_still_excluded_by_the_existing_guard():
    """A wildly-implausible ratio (e.g. a transcription slip putting the SD
    larger than the mean many times over) must still be caught by
    build_meta_analysis's pre-existing _MAX_PLAUSIBLE_CV_PCT guard - the
    derivation path is not exempt from it. Uses cv_basis="between" so the
    derived (inter) value actually lands in the basis-matching pool this
    guard checks."""
    extracted = [_cmax("P1", 1.0, sd=5.0)]  # would derive to 500%
    result = build_meta_analysis(
        derive_cv_from_mean_sd(extracted), subject_type="healthy", cv_basis="between",
    )
    assert result["powering_cv_pct"] is None
    study = result["studies"][0]
    assert study["cv_excluded_implausible"]


def test_derived_flag_survives_into_study_cv_metrics():
    extracted = [_cmax("P1", 8.09, sd=3.68)]
    result = build_meta_analysis(
        derive_cv_from_mean_sd(extracted), subject_type="healthy", cv_basis="between",
    )
    study = result["studies"][0]
    assert study["cv_metrics"] == [
        {"metric": "cv_inter_cmax", "cv_pct": 45.5, "derived_from_mean_sd": True}
    ]


def test_explicit_cv_metrics_are_flagged_not_derived():
    extracted = [
        {"parameter": "cv_intra_cmax", "value": 24.5, "unit": "%",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    result = build_meta_analysis(extracted, subject_type="healthy")
    assert result["studies"][0]["cv_metrics"][0]["derived_from_mean_sd"] is False


def test_invalid_cv_basis_is_rejected():
    extracted = [_cmax("P1", 8.09, sd=3.68)]
    try:
        build_meta_analysis(extracted, subject_type="healthy", cv_basis="sideways")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# meta_analysis_directive — disclosure text
# ---------------------------------------------------------------------------


def test_directive_discloses_a_derived_powering_cv_honestly():
    from services.meta_analysis import meta_analysis_directive

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    result = build_meta_analysis(
        derive_cv_from_mean_sd(extracted), subject_type="healthy", cv_basis="between",
    )
    directive = meta_analysis_directive(result)

    assert "45.5%" in directive
    assert "derived from that study's reported mean and SD" in directive
    assert "not a percentage the study printed directly" in directive
    assert "(derived from mean/SD)" in directive


def test_directive_does_not_claim_derived_values_were_directly_reported():
    from services.meta_analysis import meta_analysis_directive

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68)]
    result = build_meta_analysis(
        derive_cv_from_mean_sd(extracted), subject_type="healthy", cv_basis="between",
    )
    directive = meta_analysis_directive(result)

    assert "as directly reported by that study" not in directive


def test_directive_discloses_a_wrong_basis_cv_that_was_found_but_not_adopted():
    """The disclosure half of the critical fix: when a between-subject value
    is the only thing found for a within-subject-basis design, the directive
    must say so explicitly rather than silently stating "no CV was available"
    with no explanation - the model must not later invent one."""
    from services.meta_analysis import meta_analysis_directive

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    result = build_meta_analysis(
        derive_cv_from_mean_sd(extracted), subject_type="healthy", cv_basis="within",
    )
    directive = meta_analysis_directive(result)

    assert result["powering_cv_pct"] is None
    assert "45.5" in directive
    assert "between-subject" in directive
    assert "not used" in directive.lower() or "not adopted" in directive.lower()


def test_directive_for_an_explicit_cv_states_it_was_directly_reported():
    """Non-regression: for an explicit CV, the ADOPTED-CV line must say it was
    directly reported, and the table ROW for that study must carry the plain
    percentage with no '(derived from mean/SD)' marker. The trailing legend
    that EXPLAINS what the marker would mean if it appeared is separate,
    always-present boilerplate (see test_directive_legend_always_explains_the_
    derived_marker) and is deliberately not asserted against here."""
    from services.meta_analysis import meta_analysis_directive

    extracted = [
        {"parameter": "cv_intra_cmax", "value": 24.5, "unit": "%",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    result = build_meta_analysis(extracted, subject_type="healthy")
    directive = meta_analysis_directive(result)
    table_section = directive.split("Table rows", 1)[1]
    row_lines = [ln for ln in table_section.splitlines() if ln.strip().startswith("P1")]

    assert "as directly reported by that study" in directive
    assert "derived from that study's reported mean and SD" not in directive
    assert len(row_lines) == 1
    assert "(derived from mean/SD)" not in row_lines[0]
    assert "24.5%" in row_lines[0]


def test_directive_legend_always_explains_the_derived_marker():
    """The legend sentence explaining what '(derived from mean/SD)' means is
    static instructional boilerplate, present whether or not this particular
    result has a derived cell - so the model knows how to interpret the
    marker wherever it happens to appear."""
    from services.meta_analysis import meta_analysis_directive

    extracted = [
        {"parameter": "cv_intra_cmax", "value": 24.5, "unit": "%",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    result = build_meta_analysis(extracted, subject_type="healthy")
    directive = meta_analysis_directive(result)
    assert "computed here from the study's own reported mean and standard deviation" in directive


def test_summary_text_reflects_derived_provenance():
    extracted = [_cmax("DAILYMED", 8.09, sd=3.68)]
    result = build_meta_analysis(
        derive_cv_from_mean_sd(extracted), subject_type="healthy", cv_basis="between",
    )
    assert "derived from that study" in result["summary"]

    extracted2 = [
        {"parameter": "cv_intra_cmax", "value": 24.5, "unit": "%",
         "population": "healthy adults", "source_pmid": "P1", "confidence": 1.0},
    ]
    result2 = build_meta_analysis(extracted2, subject_type="healthy")
    assert "derived from that study" not in result2["summary"]


# ---------------------------------------------------------------------------
# routers/protocols.py::_resolve_powering_cv — end-to-end wiring
# ---------------------------------------------------------------------------


def test_resolve_powering_cv_biosimilar_adopts_the_derived_value():
    """A biosimilar_pk protocol always uses a parallel design and consumes its
    resolved CV with no within/between conversion (services/biostatistics.py)
    - it genuinely needs the between-subject quantity a mean/SD pair supplies,
    so the derived value IS adopted here. This is the real guselkumab case."""
    from routers.protocols import _resolve_powering_cv

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    cv_pct, provenance = _resolve_powering_cv(
        {"subject_type": "healthy"}, extracted, study_type="biosimilar_pk",
    )

    assert cv_pct == 45.5
    assert provenance["cv_source"] == "literature (highest observed)"
    assert provenance["meta_analysis"]["powering_cv_derived_from_mean_sd"] is True
    assert provenance["meta_analysis"]["cv_basis"] == "between"
    assert provenance["cv_table"], "cv_table must carry the derived study forward to the SAP"


def test_resolve_powering_cv_small_molecule_does_not_adopt_the_derived_value():
    """THE REGRESSION GUARD for the critical fix, at the router boundary: for
    any non-biosimilar study_type (BE, food_effect, ...), a mean/SD-only
    literature source must NOT silently power the study - the derived value is
    between-subject, this design needs within-subject, and there is no
    explicit within-subject CV anywhere in the extraction. Before this fix,
    this returned 45.5 (mislabelled as within-subject) for EVERY study_type;
    now it correctly falls back to None so recommend_sample_size uses its own
    clearly-flagged ASSUMED_CV_PCT instead of an inflated, wrongly-basis'd
    figure."""
    from routers.protocols import _resolve_powering_cv

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    cv_pct, provenance = _resolve_powering_cv(
        {"subject_type": "healthy"}, extracted, study_type="BE",
    )

    assert cv_pct is None
    assert "cv_source" not in provenance  # falls through to biostatistics' own "assumed" label
    assert provenance["meta_analysis"]["cv_basis"] == "within"
    assert provenance["meta_analysis"]["highest_cv_wrong_basis_pct"] == 45.5


def test_resolve_powering_cv_without_fix_would_have_found_nothing():
    """Documents the exact defect being fixed: build_meta_analysis alone
    (the pre-fix code path) finds nothing for mean/SD-only literature, so
    _resolve_powering_cv would fall through to 'assumed' without the wrapper."""
    extracted = [_cmax("DAILYMED", 8.09, sd=3.68)]
    result = build_meta_analysis(extracted, subject_type="healthy")
    assert result["powering_cv_pct"] is None


def test_resolve_powering_cv_investigator_override_still_takes_precedence():
    """Non-regression: an explicit investigator override must still win over
    ANY literature value, derived or not, regardless of study_type."""
    from routers.protocols import _resolve_powering_cv

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68)]
    cv_pct, provenance = _resolve_powering_cv(
        {"subject_type": "healthy", "cv_intra": 33.0}, extracted, study_type="biosimilar_pk",
    )
    assert cv_pct == 33.0
    assert provenance["cv_is_user_override"] is True
    assert provenance["cv_table"] == []


# ---------------------------------------------------------------------------
# _resolve_powering_cv — cv_basis must follow an explicit design override,
# not study_type alone.
#
# THE BUG (found by adversarial verification of the study_type-only version of
# this fix): cv_basis was set to "between" whenever study_type=="biosimilar_pk",
# on the assumption that a biosimilar_pk protocol always ends up parallel.
# False: recommend_sample_size's design-selection logic checks an explicit
# `requested_design` (population_config["design"]) FIRST, before its
# biosimilar-implies-parallel default. A biosimilar protocol whose design was
# explicitly overridden to "replicate" or "crossover" therefore genuinely
# needs a WITHIN-subject CV (recommend_sample_size never applies its x1.5
# within/between conversion for those designs), but the old code still handed
# it a between-subject-basis value with no conversion - silently powering the
# study on the wrong quantity, unconverted.
# ---------------------------------------------------------------------------


def test_resolve_powering_cv_biosimilar_with_no_design_override_stays_between_basis():
    """Non-regression: the common case (no explicit design) is unaffected."""
    from routers.protocols import _resolve_powering_cv

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    cv_pct, provenance = _resolve_powering_cv(
        {"subject_type": "healthy"}, extracted, study_type="biosimilar_pk",
    )
    assert cv_pct == 45.5
    assert provenance["meta_analysis"]["cv_basis"] == "between"


def test_resolve_powering_cv_biosimilar_with_replicate_override_uses_within_basis():
    """THE FIX: an explicit design="replicate" override on a biosimilar_pk
    protocol must switch cv_basis to "within" - the derived (between-subject)
    value is then correctly excluded from powering rather than silently
    adopted as if it were within-subject."""
    from routers.protocols import _resolve_powering_cv

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    cv_pct, provenance = _resolve_powering_cv(
        {"subject_type": "healthy", "design": "replicate"}, extracted, study_type="biosimilar_pk",
    )
    assert cv_pct is None
    assert provenance["meta_analysis"]["cv_basis"] == "within"
    assert provenance["meta_analysis"]["highest_cv_wrong_basis_pct"] == 45.5


def test_resolve_powering_cv_biosimilar_with_crossover_override_uses_within_basis():
    from routers.protocols import _resolve_powering_cv

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    cv_pct, provenance = _resolve_powering_cv(
        {"subject_type": "healthy", "design": "crossover"}, extracted, study_type="biosimilar_pk",
    )
    assert cv_pct is None
    assert provenance["meta_analysis"]["cv_basis"] == "within"


def test_resolve_powering_cv_biosimilar_with_parallel_override_still_uses_between_basis():
    """Non-regression: an explicit design="parallel" (redundant with the
    default) must not change the outcome."""
    from routers.protocols import _resolve_powering_cv

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    cv_pct, provenance = _resolve_powering_cv(
        {"subject_type": "healthy", "design": "parallel"}, extracted, study_type="biosimilar_pk",
    )
    assert cv_pct == 45.5
    assert provenance["meta_analysis"]["cv_basis"] == "between"


def test_resolve_powering_cv_non_biosimilar_with_parallel_override_stays_within_basis():
    """A NON-biosimilar drug explicitly designed as parallel still needs a
    within-subject CV as input - recommend_sample_size does its own x1.5
    conversion internally for that combination (design_key=="parallel" and
    not biosimilar), so cv_basis must stay "within" here, not flip to
    "between"."""
    from routers.protocols import _resolve_powering_cv

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    cv_pct, provenance = _resolve_powering_cv(
        {"subject_type": "healthy", "design": "parallel"}, extracted, study_type="BE",
    )
    assert cv_pct is None
    assert provenance["meta_analysis"]["cv_basis"] == "within"


def test_biosimilar_replicate_override_no_longer_silently_underpowers():
    """End-to-end regression guard, independently reproduced during code
    review: before this fix, a biosimilar_pk + design="replicate" override fed
    the unconverted between-subject-derived CV straight into
    recommend_sample_size, giving n_enrolled=124 - roughly HALF of the
    parallel-design n_enrolled=240 the same CV correctly produces when used as
    the between-subject quantity it actually is. After the fix, with no
    explicit within-subject CV available, the design correctly falls back to
    ASSUMED_CV_PCT instead of silently halving the enrollment target."""
    from routers.protocols import _resolve_powering_cv
    from services.biostatistics import recommend_sample_size, ASSUMED_CV_PCT

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    cv_pct, provenance = _resolve_powering_cv(
        {"subject_type": "healthy", "design": "replicate"}, extracted, study_type="biosimilar_pk",
    )
    rec = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="examplemab",
        cv_intra_pct=cv_pct, cv_source=provenance.get("cv_source"),
        requested_design="replicate",
    )
    assert rec.design_key == "replicate"
    assert rec.cv_pct == ASSUMED_CV_PCT
    assert rec.cv_source == "assumed"


# ---------------------------------------------------------------------------
# services/biostatistics.py::recommend_sample_size — the real-world effect of
# the fix on the number that ends up in the protocol. These pin the exact
# figures independently confirmed during code review.
# ---------------------------------------------------------------------------


def test_biosimilar_sample_size_uses_the_derived_cv_directly():
    from routers.protocols import _resolve_powering_cv
    from services.biostatistics import recommend_sample_size

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    cv_pct, provenance = _resolve_powering_cv(
        {"subject_type": "healthy"}, extracted, study_type="biosimilar_pk",
    )
    rec = recommend_sample_size(
        study_type="biosimilar_pk", drug_inn="guselkumab",
        cv_intra_pct=cv_pct, cv_source=provenance.get("cv_source"),
    )
    assert rec.design_key == "parallel"
    assert rec.cv_pct == 45.5
    assert rec.n_evaluable == 222


def test_small_molecule_crossover_falls_back_to_assumed_cv_not_the_derived_one():
    """Before the fix, this scenario fed the between-subject 45.5% figure into
    a crossover design as if it were within-subject, giving n=112 for a study
    that should need n=54 at the true (unknown here) within-subject CV - a
    >2x oversized trial. After the fix, with no explicit within-subject CV
    available, the design correctly falls back to ASSUMED_CV_PCT (25.0%)."""
    from routers.protocols import _resolve_powering_cv
    from services.biostatistics import recommend_sample_size, ASSUMED_CV_PCT

    extracted = [_cmax("DAILYMED", 8.09, sd=3.68, population="healthy adult subjects")]
    cv_pct, provenance = _resolve_powering_cv(
        {"subject_type": "healthy"}, extracted, study_type="BE",
    )
    rec = recommend_sample_size(
        study_type="BE", drug_inn="some-small-molecule",
        cv_intra_pct=cv_pct, cv_source=provenance.get("cv_source"),
    )
    assert rec.design_key == "crossover"
    assert rec.cv_pct == ASSUMED_CV_PCT
    assert rec.cv_source == "assumed"


# ---------------------------------------------------------------------------
# services/ai_service.py — the extraction tool schema and prompt actually
# tell the model to transcribe mean+sd rather than skip or self-calculate
# ---------------------------------------------------------------------------


def test_extraction_tool_schema_has_an_sd_field():
    from services.ai_service import _PK_EXTRACTION_TOOL

    props = _PK_EXTRACTION_TOOL["input_schema"]["properties"]["parameters"]["items"]["properties"]
    assert "sd" in props
    assert props["sd"]["type"] == "number"


def test_extraction_tool_description_tells_the_model_to_transcribe_not_calculate():
    from services.ai_service import _PK_EXTRACTION_TOOL

    desc = _PK_EXTRACTION_TOOL["description"]
    assert "mean" in desc.lower() and "sd" in desc.lower()
    assert "`sd` field" in desc
    # The critical instruction: derive in Python, never in the model.
    assert "never to do that division yourself" in desc


def test_extraction_prompt_carries_the_mean_sd_rule():
    from services.ai_service import _build_extraction_prompt

    prompt = _build_extraction_prompt([], "guselkumab")
    assert "mean (+/- SD)" in prompt
    assert "`sd` field" in prompt
    assert "not a gap to skip" in prompt
    # Must be explicit this is not DailyMed-only - journal abstracts too.
    assert "not only DAILYMED" in prompt


def test_extraction_prompt_still_carries_all_prior_rules():
    """Non-regression: adding rule 8 must not have disturbed rules 1-7."""
    from services.ai_service import _build_extraction_prompt

    prompt = _build_extraction_prompt(
        [{"pmid": "123", "title": "T", "abstract": "A"}], "metoprolol",
    )
    assert "source_pmid must be an exact match" in prompt
    assert "cv_intra" in prompt
    assert "DAILYMED" in prompt
    assert "Normalize time-based parameters" in prompt
    assert "PMID: 123" in prompt and "Title: T" in prompt and "Abstract: A" in prompt
