"""Tests for the bioanalytical method validation (BMV) planning engine.

The engine is a deterministic knowledge layer: given the drug's real PK records
and the real label text, it must return an assay package a pharmacologist can
defend line by line. These tests pin the things whose failure would be a genuine
CLINICAL error rather than a cosmetic one:

* the calibration range must bracket the drug's own anticipated Cmax - a ULOQ
  below Cmax loses the peak sample, an LLOQ above 5% of Cmax truncates AUC;
* a unit conversion must never be silently wrong (ug/mL vs ng/mL is a 1000-fold
  error in the range, and a molar unit must be refused, not assumed);
* with no Cmax the engine must REFUSE to invent a range rather than guess;
* a ligand-binding assay must not inherit chromatographic acceptance criteria
  (or vice versa) - the numbers genuinely differ;
* a racemate in the label must NOT be turned into an automatic requirement for
  an enantiomer-specific assay;
* a whole-blood analyte must not be centrifuged into plasma;
* everything must round-trip through json.dumps (it is persisted to JSONB) and
  stay pure ASCII (this repo's Windows console is cp1252).

AND ONE THING THE ENGINE MUST *NOT* PUT IN THE PROTOCOL
-------------------------------------------------------
A practising clinical pharmacologist reviewing real generated protocols asked
for the method-validation content to come OUT: "Exclude items related to the
statistical analysis plan (SAP) in the protocol as they are not needed.
Bioanalytical method is in SAP / QC samples also in SAP / Stability Battery /
Incurred Sample Reanalysis (ISR)."

That is not tidiness. The Bioanalytical Method Validation report and the SAP are
separate documents on their own approval cycles. Fixing their numbers in the
clinical protocol creates a second, uncontrolled copy of them: the moment the
validated method differs from what the protocol committed to, the study has a
protocol deviation on a point the protocol had no business deciding.

So the contract is two-sided, and the tests below pin BOTH sides - the material
is ABSENT from the prompt directive, and still PRESENT in the engine's result
(analyte selection and dilution-integrity reasoning are derived from the
calibration arithmetic, and services/protocol_qc.py and services/crf_dmp.py read
those keys). Deleting the data model to pass the first half would be a
regression, not a fix.
"""
from __future__ import annotations

import json
import re

import pytest

from services.bioanalytical import (
    BENCHTOP_STABILITY_HOURS,
    CHROMATOGRAPHIC,
    DEFAULT_STORAGE_TEMP_C,
    DILUTION_INTEGRITY_FACTOR,
    FREEZE_THAW_CYCLES,
    ISR_FIRST_TIER_PCT,
    ISR_FIRST_TIER_SAMPLES,
    ISR_MIN_PASS_PCT,
    ISR_SECOND_TIER_PCT,
    LIGAND_BINDING,
    LLOQ_FRACTION_OF_CMAX,
    LONG_TERM_STABILITY_MONTHS,
    PROCESSED_SAMPLE_STABILITY_HOURS,
    ULOQ_MARGIN_FACTOR,
    _PARAMETERS,
    _STABILITY,
    bioanalytical_directive,
    isr_sample_count,
    plan_bioanalytical,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _cmax(value: float, unit: str = "ng/mL", pmid: str = "12345678") -> dict:
    """A PK record shaped exactly like routers/protocols.py::_build_context emits
    (models.PKParameter.cmax serialises to the string 'Cmax')."""
    return {
        "parameter": "Cmax",
        "value": value,
        "unit": unit,
        "population": "healthy adults, fasted",
        "source_pmid": pmid,
        "confidence": 0.9,
    }


PLAIN_LABEL = {
    "clinical_pharmacology": (
        "Following oral administration the drug is rapidly absorbed. It is widely "
        "distributed and eliminated primarily by hepatic metabolism."
    )
}


def _strings(obj, path=""):
    """Every string in the result tree, with its path (for readable failures)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


# ---------------------------------------------------------------------------
# Catalogue integrity
# ---------------------------------------------------------------------------


def test_parameter_catalogue_is_well_formed():
    keys = [p.key for p in _PARAMETERS]
    assert len(keys) == len(set(keys)), "duplicate BMV parameter keys"
    for p in _PARAMETERS:
        assert p.category in ("performance", "conduct"), p.key
        assert p.assay_classes in ("both", "chromatographic", "ligand_binding"), p.key
        assert p.label and p.requirement and p.acceptance and p.reference
        # A requirement without a defensible reason is a checkbox, not a plan.
        assert len(p.rationale) > 80, f"{p.key} rationale too thin to justify a row"


def test_full_bmv_parameter_set_is_present():
    """The FDA BMV / ICH M10 core set. A missing one is a validation gap the
    laboratory would only discover at report review."""
    keys = {p.key for p in _PARAMETERS}
    for required in (
        "selectivity",
        "specificity",
        "matrix_effect",
        "recovery",
        "calibration_curve",
        "accuracy_precision_within_run",
        "accuracy_precision_between_run",
        "carryover",
        "dilution_integrity",
    ):
        assert required in keys, f"BMV parameter missing: {required}"


def test_stability_battery_covers_the_full_required_set():
    keys = {s.key for s in _STABILITY}
    for required in (
        "bench_top",       # short-term
        "freeze_thaw",
        "long_term",       # frozen, at the stated storage temperature
        "processed_sample",  # autosampler
        "stock_solution",
    ):
        assert required in keys, f"stability test missing: {required}"


@pytest.mark.parametrize("is_biologic", [False, True])
def test_no_unrendered_placeholder_reaches_the_protocol(is_biologic):
    """Catalogue text carries {tokens} filled from the resolved criteria. A
    missing token would ship literal '+/-{re}%' into a regulatory document."""
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)],
        label=PLAIN_LABEL,
        drug_inn="testdrug",
        study_type="biosimilar_pk" if is_biologic else "BE",
        is_biologic=is_biologic,
    )
    leftovers = [
        (path, s) for path, s in _strings(plan)
        if re.search(r"\{[a-z_]+\}", s)
    ]
    assert not leftovers, f"unrendered placeholders: {leftovers[:3]}"


# ---------------------------------------------------------------------------
# Assay class -> acceptance criteria  (swapping these is a real error)
# ---------------------------------------------------------------------------


def test_chromatographic_criteria_are_the_lc_ms_ms_numbers():
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL,
        drug_inn="atorvastatin", study_type="BE", is_biologic=False,
    )
    crit = plan["criteria"]
    assert plan["assay_class"] == "chromatographic"
    assert crit["re_pct"] == 15.0 and crit["cv_pct"] == 15.0
    assert crit["re_lloq_pct"] == 20.0 and crit["cv_lloq_pct"] == 20.0
    assert crit["isr_window_pct"] == 20.0


def test_ligand_binding_criteria_are_relaxed_for_a_biologic():
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(30.0, "ug/mL")], label=PLAIN_LABEL,
        drug_inn="adalimumab", study_type="biosimilar_pk",
    )
    crit = plan["criteria"]
    assert plan["assay_class"] == "ligand_binding"
    assert crit["re_pct"] == 20.0 and crit["cv_pct"] == 20.0
    assert crit["re_lloq_pct"] == 25.0 and crit["cv_lloq_pct"] == 25.0
    assert crit["isr_window_pct"] == 30.0
    # An LBA also carries a total-error cap, which a chromatographic assay does not.
    assert crit["total_error_pct"] is not None
    assert CHROMATOGRAPHIC.total_error_pct is None


def test_a_biologic_never_inherits_chromatographic_criteria():
    """The single most damaging mix-up: holding an ELISA to +/-15%/15% CV would
    fail a method that is perfectly acceptable, and quoting a 20% ISR window for
    a ligand-binding assay understates what ICH M10 allows."""
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(30.0, "ug/mL")], label=PLAIN_LABEL,
        drug_inn="some-new-mab", study_type="biosimilar_pk", is_biologic=True,
    )
    assert plan["criteria"]["re_pct"] != CHROMATOGRAPHIC.re_pct
    assert plan["isr"]["agreement_window_pct"] == LIGAND_BINDING.isr_window_pct
    text = plan["summary"]
    assert "15%" not in text, "chromatographic numbers leaked into a biologic plan"


def test_explicit_modality_overrides_every_other_signal():
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL,
        drug_inn="adalimumab", study_type="biosimilar_pk", is_biologic=False,
    )
    assert plan["assay_class"] == "chromatographic"
    assert "Caller-supplied" in plan["assay_class_basis"]


def test_unclassifiable_drug_defaults_to_chromatographic_and_says_so():
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(50.0)], label=PLAIN_LABEL, drug_inn="zzqqxx",
    )
    assert plan["assay_class"] == "chromatographic"
    assert plan["assay_class_basis"]


# ---------------------------------------------------------------------------
# Calibration range - derived from the drug's own anticipated Cmax
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmax_ng", [0.05, 1.0, 9.99, 10.0, 55.0, 137.4, 999.0, 1000.0, 48250.0]
)
def test_calibration_range_brackets_the_anticipated_cmax(cmax_ng):
    """ULOQ above Cmax with the stated margin; LLOQ at or below 5% of Cmax.

    A ULOQ below Cmax sends every peak sample above the curve; an LLOQ above 5%
    of Cmax truncates AUC0-t and biases the exposure ratio. Rounding must never
    push either limit the wrong way.
    """
    cal = plan_bioanalytical(pk_parameters=[_cmax(cmax_ng)])["calibration"]
    assert cal["derived"] is True
    assert cal["uloq"] >= cmax_ng * ULOQ_MARGIN_FACTOR - 1e-9
    assert 0 < cal["lloq"] <= cmax_ng * LLOQ_FRACTION_OF_CMAX + 1e-12
    # >= 4.3 half-lives of quantifiable decline is what AUC0-t >= 80% of
    # AUC0-inf requires; that is the whole point of the 5% anchor.
    assert cal["half_lives_quantifiable"] >= 4.3


def test_qc_levels_sit_inside_the_range_and_in_order():
    cal = plan_bioanalytical(pk_parameters=[_cmax(200.0)])["calibration"]
    concs = [q["concentration"] for q in cal["qc_levels"]]
    assert cal["lloq"] <= concs[0] < concs[1] < concs[2] < concs[3] < cal["uloq"]
    # High QC at >= 75% of the ULOQ, low QC within 3x the LLOQ (ICH M10).
    assert concs[3] >= 0.75 * cal["uloq"]
    assert concs[1] <= 3.0 * cal["lloq"] + 1e-9


def test_highest_reported_cmax_drives_the_range():
    """The range must cover the most exposed subject, so the engine takes the
    HIGHEST literature Cmax, not the mean of the reports."""
    cal = plan_bioanalytical(
        pk_parameters=[_cmax(50.0, pmid="1"), _cmax(300.0, pmid="2"), _cmax(90.0, pmid="3")]
    )["calibration"]
    assert cal["anticipated_cmax"]["value"] == pytest.approx(300.0)
    assert cal["anticipated_cmax"]["source_pmid"] == "2"
    assert cal["uloq"] >= 600.0


def test_micrograms_per_ml_is_converted_not_taken_at_face_value():
    """0.4 ug/mL is 400 ng/mL. Getting this wrong is a 1000-fold error in the
    calibration range - the assay would be built for the wrong drug."""
    cal = plan_bioanalytical(pk_parameters=[_cmax(0.4, "ug/mL")])["calibration"]
    assert cal["anticipated_cmax"]["value"] == pytest.approx(400.0)
    assert cal["uloq"] >= 800.0


def test_micro_sign_unit_is_not_silently_truncated_to_grams():
    """'ug/mL' written with the real micro sign must not lose the prefix and
    become g/mL - that would be a 1,000,000-fold error."""
    micro = chr(0xB5) + "g/mL"   # U+00B5 MICRO SIGN, kept out of the source as ASCII
    cal = plan_bioanalytical(pk_parameters=[_cmax(2.0, micro)])["calibration"]
    assert cal["anticipated_cmax"]["value"] == pytest.approx(2000.0)


def test_molar_units_are_refused_rather_than_assumed():
    """nM cannot be converted without a molecular weight. Assuming ng/mL would
    silently produce a range that is wrong by the molecular weight."""
    plan = plan_bioanalytical(pk_parameters=[_cmax(50.0, "nM")])
    cal = plan["calibration"]
    assert cal["derived"] is False
    assert cal["lloq"] is None and cal["uloq"] is None
    assert any(r["reason"] for r in cal["rejected_records"])
    assert {f["key"] for f in plan["flags"]} >= {"cmax_unavailable", "cmax_units_unconvertible"}


def test_missing_cmax_refuses_to_invent_a_range_and_flags_it():
    plan = plan_bioanalytical(pk_parameters=[], label=PLAIN_LABEL, drug_inn="testdrug")
    cal = plan["calibration"]
    assert cal["derived"] is False
    assert cal["lloq"] is None and cal["uloq"] is None
    flag = next(f for f in plan["flags"] if f["key"] == "cmax_unavailable")
    assert flag["severity"] == "high"
    assert "cannot be derived" in cal["statement"].lower() or "not established" in cal["statement"].lower()
    # The margin rules are still stated, so the range can be set later correctly.
    assert "2-fold" in cal["statement"] and "5%" in cal["statement"]


def test_non_cmax_records_do_not_drive_the_range():
    plan = plan_bioanalytical(pk_parameters=[
        {"parameter": "t_half", "value": 7.0, "unit": "h", "source_pmid": "1"},
        {"parameter": "AUC0t", "value": 900.0, "unit": "ng*h/mL", "source_pmid": "1"},
    ])
    assert plan["calibration"]["derived"] is False


def test_planned_dose_is_disclosed_not_used_to_rescale_silently():
    """The literature Cmax carries no dose, so dose-proportional rescaling would
    be an invention. The engine records the dose and asks for confirmation."""
    plan = plan_bioanalytical(pk_parameters=[_cmax(100.0)], dose_mg=80.0)
    assert plan["planned_dose_mg"] == 80.0
    assert plan["calibration"]["uloq"] == plan_bioanalytical(
        pk_parameters=[_cmax(100.0)])["calibration"]["uloq"]
    assert any(f["key"] == "dose_scaling_unverified" for f in plan["flags"])


# ---------------------------------------------------------------------------
# Dilution integrity
# ---------------------------------------------------------------------------


def test_dilution_integrity_is_required_and_names_its_factor():
    plan = plan_bioanalytical(pk_parameters=[_cmax(100.0)], is_biologic=False)
    dil = plan["dilution"]
    assert dil["required"] is True
    assert dil["factor"] == DILUTION_INTEGRITY_FACTOR
    assert dil["detail"]["key"] == "dilution_integrity"
    assert str(DILUTION_INTEGRITY_FACTOR) in dil["detail"]["requirement"]
    # A sample above the ULOQ is only reportable if the dilution was validated
    # in advance, so the reportable ceiling has to be stated.
    assert "ULOQ" in dil["reportable_ceiling"]


def test_ligand_binding_uses_dilutional_linearity_and_parallelism():
    """An LBA measures binding, not mass: parallelism against incurred samples is
    the control that matters, and a hook effect must be excluded."""
    plan = plan_bioanalytical(pk_parameters=[_cmax(30.0, "ug/mL")], is_biologic=True)
    dil = plan["dilution"]
    assert dil["detail"]["key"] == "dilutional_linearity_parallelism"
    assert "parallelism" in dil["detail"]["requirement"].lower()
    assert "hook" in dil["detail"]["acceptance"].lower()


# ---------------------------------------------------------------------------
# Incurred sample reanalysis
# ---------------------------------------------------------------------------


def test_isr_sample_count_follows_the_tiered_rule():
    assert isr_sample_count(0) == 0
    assert isr_sample_count(1) == 1                      # rounded up: never zero
    assert isr_sample_count(400) == 40                   # 10% of the first tier
    assert isr_sample_count(ISR_FIRST_TIER_SAMPLES) == 100
    assert isr_sample_count(1600) == 130                 # 100 + 5% of 600
    assert isr_sample_count(1005) == 101                 # fractional sample rounds up


def test_isr_count_is_monotonic_in_study_size():
    counts = [isr_sample_count(n) for n in range(0, 3000, 37)]
    assert counts == sorted(counts)


def test_isr_acceptance_criterion_matches_the_assay_class():
    chrom = plan_bioanalytical(pk_parameters=[_cmax(100.0)], is_biologic=False)["isr"]
    lba = plan_bioanalytical(pk_parameters=[_cmax(100.0)], is_biologic=True)["isr"]
    assert chrom["required"] is True and lba["required"] is True
    assert chrom["min_pass_pct"] == ISR_MIN_PASS_PCT == 67.0
    assert chrom["agreement_window_pct"] == 20.0
    assert lba["agreement_window_pct"] == 30.0
    assert f"{ISR_FIRST_TIER_PCT:.0f}%" in chrom["rule"]
    assert f"{ISR_SECOND_TIER_PCT:.0f}%" in chrom["rule"]
    # Repeats come from the back-up aliquot, in a separate run.
    assert "back-up aliquot" in chrom["conduct"]


# ---------------------------------------------------------------------------
# Analyte selection - grounded in the real label text
# ---------------------------------------------------------------------------


def test_plain_label_measures_the_parent_drug():
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL, drug_inn="testdrug")
    analytes = plan["analytes"]
    assert analytes["be_basis"] == "parent drug"
    assert "testdrug" in analytes["primary"][0]["analyte"]
    assert analytes["additional"] == []


def test_prodrug_label_moves_the_conclusion_to_the_active_moiety():
    label = {"clinical_pharmacology": (
        "Fosdrugil is a prodrug that is rapidly hydrolyzed by intestinal alkaline "
        "phosphatase to the active moiety drugil."
    )}
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=label, drug_inn="fosdrugil")
    analytes = plan["analytes"]
    assert analytes["be_basis"] == "active moiety"
    assert "active moiety" in analytes["primary"][0]["analyte"].lower()
    # The intact prodrug is still measured where quantifiable, as supportive data.
    assert any("prodrug" in a["analyte"].lower() for a in analytes["additional"])
    assert analytes["primary"][0]["quote"], "the triggering label text must be quoted"
    assert any(f["key"] == "prodrug_detected" for f in plan["flags"])


def test_active_metabolite_is_added_as_a_supporting_analyte():
    label = {"clinical_pharmacology": (
        "The major circulating metabolite is pharmacologically active and "
        "contributes to the efficacy of the drug."
    )}
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=label, drug_inn="testdrug")
    analytes = plan["analytes"]
    assert analytes["be_basis"] == "parent drug", (
        "bioequivalence is concluded on the parent; the metabolite is supportive")
    assert any("metabolite" in a["analyte"].lower() for a in analytes["additional"])


def test_ordinary_metabolism_is_not_read_as_a_prodrug():
    """'Converted to active metabolites' is ordinary metabolism. Reading it as
    prodrug language would move the bioequivalence conclusion off the parent
    drug - and the parent is where the product-specific guidance puts it."""
    label = {"clinical_pharmacology": (
        "Atorvastatin is converted to active ortho- and parahydroxylated "
        "metabolites. Approximately 70% of circulating inhibitory activity is "
        "attributed to active metabolites."
    )}
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(27.0)], label=label, drug_inn="atorvastatin")
    assert plan["analytes"]["be_basis"] == "parent drug"
    assert not any(f["key"] == "prodrug_detected" for f in plan["flags"])
    # The active metabolite is still picked up - as a supporting analyte.
    assert any("metabolite" in a["analyte"].lower()
               for a in plan["analytes"]["additional"])


def test_explicit_prodrug_wording_still_detected_alongside_a_metabolite():
    label = {"clinical_pharmacology": (
        "Fosinopril is a prodrug that is hydrolyzed by esterases to the active "
        "metabolite fosinoprilat."
    )}
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(50.0)], label=label, drug_inn="fosinopril")
    assert plan["analytes"]["be_basis"] == "active moiety"


def test_negated_metabolite_language_does_not_add_an_analyte():
    """'There are no active metabolites' is REASSURANCE. Reading it as a
    requirement would add an analyte that does not exist."""
    label = {"clinical_pharmacology": (
        "There are no active metabolites. The drug is metabolized to inactive "
        "metabolites that are excreted renally."
    )}
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=label, drug_inn="testdrug")
    assert plan["analytes"]["additional"] == []
    assert "active_metabolite" not in plan["analytes"]["detected_cues"]


def test_exactly_one_analyte_carries_the_bioequivalence_conclusion():
    """Two 'primary' analytes in a protocol is a review question, not a stronger
    plan: a reviewer must be able to see which analyte decides bioequivalence."""
    label = {"clinical_pharmacology": (
        "Fosdrugil is a prodrug converted in vivo to the active moiety. The active "
        "metabolite contributes to the pharmacologic effect."
    )}
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=label, drug_inn="fosdrugil")
    analytes = plan["analytes"]
    primaries = [
        a for a in analytes["primary"] + analytes["additional"]
        if a["role"] == "primary"
    ]
    assert len(primaries) == 1, f"expected one primary analyte, got {primaries}"


def test_racemate_does_not_automatically_require_a_chiral_assay():
    """The FDA default is an ACHIRAL assay of the racemate; an enantiomer-specific
    method is recommended only when all four conditions are met. Auto-switching
    would impose a slower, harder validation with no interpretive gain."""
    label = {"clinical_pharmacology": (
        "Ibuprofen is a racemic mixture of the S(+) and R(-) enantiomers; the "
        "R-enantiomer undergoes unidirectional inversion in vivo."
    )}
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=label, drug_inn="ibuprofen")
    chiral = plan["analytes"]["chiral_assessment"]
    assert chiral["racemate_detected"] is True
    assert "achiral" in chiral["assay"].lower()
    assert len(chiral["conditions_for_chiral_assay"]) == 4
    assert chiral["quote"], "the racemate finding must cite the label text"
    flag = next(f for f in plan["flags"] if f["key"] == "racemate_detected")
    assert flag["severity"] == "low"
    # And the prompt must be told not to overclaim it.
    directive = bioanalytical_directive(plan)
    assert "ACHIRAL" in directive


def test_non_chiral_label_leaves_the_chiral_assessment_negative():
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL, drug_inn="testdrug")
    assert plan["analytes"]["chiral_assessment"]["racemate_detected"] is False


# ---------------------------------------------------------------------------
# Sample handling and chain of custody
# ---------------------------------------------------------------------------


def test_plasma_handling_chain_is_complete():
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL,
        drug_inn="testdrug", is_biologic=False)
    handling = plan["sample_handling"]
    assert handling["matrix"] == "Plasma"
    assert "EDTA" in handling["collection_tube"]
    steps = {s["key"] for s in handling["steps"]}
    assert {"collection", "cooling", "centrifugation", "aliquoting",
            "labelling", "freezing", "storage", "shipment"} <= steps
    assert handling["storage_temp_c"] == DEFAULT_STORAGE_TEMP_C
    assert handling["aliquots_per_sample"] >= 2, "a back-up aliquot is what makes ISR possible"
    centrifuge = next(s for s in handling["steps"] if s["key"] == "centrifugation")
    for token in ("x g", "minutes"):
        assert token in centrifuge["detail"]
    assert "dry ice" in next(
        s for s in handling["steps"] if s["key"] == "shipment")["detail"]


def test_whole_blood_analyte_is_never_centrifuged_into_plasma():
    """Tacrolimus partitions into erythrocytes and is measured in whole blood.
    Assaying plasma would produce a number comparable with no reference value."""
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(15.0)], label=PLAIN_LABEL, drug_inn="tacrolimus")
    handling = plan["sample_handling"]
    assert handling["matrix"] == "Whole blood"
    steps = {s["key"] for s in handling["steps"]}
    assert "centrifugation" not in steps
    assert "no_separation" in steps
    # Whole-blood stability becomes mandatory in that case.
    wb = next(s for s in plan["stability"] if s["key"] == "whole_blood")
    assert wb["applicable"] is True


def test_serum_is_not_chilled_before_the_clot_has_formed():
    """A ligand-binding assay is run in serum. Icing a clot-activator tube before
    clotting leaves fibrin strands that bias every volume-dependent step."""
    plan = plan_bioanalytical(pk_parameters=[_cmax(30.0, "ug/mL")], is_biologic=True)
    handling = plan["sample_handling"]
    assert handling["matrix"] == "Serum"
    clot = next(s for s in handling["steps"] if s["key"] == "clotting")
    assert "NOT" in clot["detail"] and "ice" in clot["detail"].lower()


def test_erythrocyte_partitioning_signal_is_flagged_for_an_unlisted_drug():
    label = {"clinical_pharmacology": (
        "The drug distributes into red blood cells, with a blood-to-plasma ratio of 15."
    )}
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(20.0)], label=label, drug_inn="newagent")
    assert any(f["key"] == "erythrocyte_partitioning" for f in plan["flags"])


def test_chain_of_custody_is_specified():
    plan = plan_bioanalytical(pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL)
    custody = {c["key"] for c in plan["sample_handling"]["chain_of_custody"]}
    assert {"manifest", "receipt", "accountability", "excursions"} <= custody
    joined = " ".join(c["requirement"] for c in plan["sample_handling"]["chain_of_custody"])
    assert "Part 11" in joined


def test_whole_blood_stability_is_triggered_by_an_ex_vivo_instability_signal():
    label = {"clinical_pharmacology": (
        "The lactone form interconverts with the hydroxy acid; the acyl glucuronide "
        "is subject to back-conversion during sample processing."
    )}
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=label, drug_inn="testdrug")
    wb = next(s for s in plan["stability"] if s["key"] == "whole_blood")
    assert wb["applicable"] is True
    assert any(f["key"] == "ex_vivo_instability_signal" for f in plan["flags"])


def test_whole_blood_stability_is_not_demanded_without_a_signal():
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL, drug_inn="testdrug")
    wb = next(s for s in plan["stability"] if s["key"] == "whole_blood")
    assert wb["applicable"] is False
    assert "Not triggered" in wb["note"]


def test_stability_conditions_carry_concrete_numbers():
    plan = plan_bioanalytical(pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL)
    by_key = {s["key"]: s for s in plan["stability"]}
    assert re.search(r"\d+\s*hours", by_key["bench_top"]["condition"])
    assert re.search(r"\d+ freeze-thaw cycles", by_key["freeze_thaw"]["condition"])
    assert re.search(r"\d+ months", by_key["long_term"]["condition"])
    # The long-term claim must name the storage temperature it applies to.
    assert f"{DEFAULT_STORAGE_TEMP_C:.0f}" in by_key["long_term"]["condition"]
    assert re.search(r"\d+\s*hours", by_key["processed_sample"]["condition"])


# ---------------------------------------------------------------------------
# Laboratory qualification and immunogenicity
# ---------------------------------------------------------------------------


def test_laboratory_is_a_placeholder_and_never_invented():
    plan = plan_bioanalytical(pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL)
    lab = plan["lab_qualification"]
    assert lab["facility_named"] is False
    assert "[" in lab["facility"] and "]" in lab["facility"]
    reqs = {r["key"] for r in lab["requirements"]}
    assert {"quality_system", "validation_before_analysis", "part11"} <= reqs
    assert any(f["key"] == "facility_not_named" for f in plan["flags"])
    # The prompt must be told not to fill the placeholder with a plausible name.
    assert "do not invent a laboratory name" in bioanalytical_directive(plan)


def test_cross_validation_plan_exists_for_a_multi_laboratory_study():
    plan = plan_bioanalytical(pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL)
    xval = plan["lab_qualification"]["cross_validation"]
    assert "more than one laboratory" in xval["required_when"].lower()
    assert re.search(r"at least \d+ study samples", xval["design"])
    assert xval["acceptance"] and xval["rationale"]


def test_immunogenicity_package_is_biologic_only():
    small = plan_bioanalytical(pk_parameters=[_cmax(100.0)], is_biologic=False)
    bio = plan_bioanalytical(
        pk_parameters=[_cmax(30.0, "ug/mL")], study_type="biosimilar_pk", is_biologic=True)
    assert small["immunogenicity"] is None
    tiers = [t["tier"] for t in bio["immunogenicity"]["tiers"]]
    assert tiers[:2] == ["Screening", "Confirmatory"]
    assert any("Neutralising" in t for t in tiers)
    # Drug tolerance is the parameter that most often invalidates an ADA dataset.
    assert any("tolerance" in e.lower() for e in bio["immunogenicity"]["validation_elements"])


# ---------------------------------------------------------------------------
# Disclosure of assumptions
# ---------------------------------------------------------------------------


def test_named_constants_are_surfaced_for_disclosure():
    """Every assumption the engine makes has to be visible in the protocol, not
    buried in the code."""
    plan = plan_bioanalytical(pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL)
    named = {a["constant"] for a in plan["assumptions"]}
    assert {"ULOQ_MARGIN_FACTOR", "LLOQ_FRACTION_OF_CMAX",
            "DILUTION_INTEGRITY_FACTOR", "DEFAULT_STORAGE_TEMP_C"} <= named
    margin = next(a for a in plan["assumptions"] if a["constant"] == "ULOQ_MARGIN_FACTOR")
    assert margin["value"] == ULOQ_MARGIN_FACTOR
    assert margin["why"]


def test_undetermined_range_is_disclosed_as_an_assumption_too():
    plan = plan_bioanalytical(pk_parameters=[])
    assert any(a["constant"] == "calibration_range" for a in plan["assumptions"])


# ---------------------------------------------------------------------------
# Serialisation, encoding, and the prompt directive
# ---------------------------------------------------------------------------


def test_result_is_json_serialisable_for_population_config():
    """It is persisted inside population_config (JSONB), so it must round-trip."""
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(0.4, "ug/mL")],
        label={"clinical_pharmacology": "A racemic prodrug with an active metabolite."},
        drug_inn="testdrug", study_type="BE", dose_mg=10.0,
    )
    restored = json.loads(json.dumps(plan))
    assert restored["calibration"]["uloq"] == plan["calibration"]["uloq"]
    assert restored["analytes"]["be_basis"] == plan["analytes"]["be_basis"]
    assert restored["parameters"][0]["key"] == plan["parameters"][0]["key"]


@pytest.mark.parametrize("is_biologic", [False, True])
def test_output_is_pure_ascii(is_biologic):
    """This repo has been bitten by cp1252 console encode errors, and a real
    DailyMed label is full of registered-trademark signs, micro signs and en
    dashes. Anything the engine ECHOES from the label, the unit or the PK record
    goes on into the prompt, the DOCX export and the Windows log, so none of it
    may carry a non-ASCII byte.
    """
    micro, en_dash, reg, plus_minus = chr(0xB5), chr(0x2013), chr(0xAE), chr(0xB1)
    record = _cmax(2.0, micro + "g/mL")
    record["population"] = f"healthy adults, 18{en_dash}45 y (mean {plus_minus} SD)"
    plan = plan_bioanalytical(
        pk_parameters=[record],
        label={"clinical_pharmacology": (
            f"Drugil{reg} is a racemic prodrug converted in vivo to the active "
            f"moiety; the active metabolite circulates at 30{en_dash}40 "
            f"{micro}g/mL. The lactone form interconverts ex vivo."
        )},
        drug_inn="tacrolimus", is_biologic=is_biologic,
    )
    offenders = [
        (path, s) for path, s in _strings(plan) if any(ord(c) > 127 for c in s)
    ]
    assert not offenders, f"non-ASCII in engine output: {offenders[:3]}"
    directive = bioanalytical_directive(plan)
    assert all(ord(c) < 128 for c in directive)
    # Folding must not DROP the micro prefix - that would read as g/mL.
    assert plan["calibration"]["anticipated_cmax"]["reported_as"] == "2 ug/mL"
    assert plan["calibration"]["anticipated_cmax"]["value"] == pytest.approx(2000.0)


def test_directive_locks_the_facts_for_the_prompt():
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL, drug_inn="testdrug")
    directive = bioanalytical_directive(plan)
    assert "ALREADY DETERMINED" in directive
    assert "do NOT invent" in directive
    assert "Matrix:" in directive
    # Stability battery and incurred sample reanalysis are Bioanalytical Method
    # Validation / Statistical Analysis Plan content - deliberately excluded from
    # the clinical-protocol directive. See
    # test_directive_never_puts_a_numeric_calibration_range_in_the_protocol.
    for token in ("Incurred sample reanalysis", "Stability battery"):
        assert token not in directive
    # Empty input yields no directive rather than a misleading empty header.
    assert bioanalytical_directive({}) == ""


def test_directive_never_puts_a_numeric_calibration_range_in_the_protocol():
    """Setting the LLOQ/ULOQ is the analytical laboratory's job, recorded in the
    Bioanalytical Method Validation report - a separate, separately-approved
    document. It is out of scope for the clinical protocol.

    This is pinned because deriving it here was also getting it WRONG: for a
    biologic whose published Cmax is reported in ug/mL, the 5%-of-Cmax anchor
    produced an LLOQ ABOVE the Cmax, which the protocol's own QC then flagged as
    a FAIL. The engine still derives the range internally (it drives analyte
    selection and dilution-integrity reasoning) - it just must not be asserted
    as protocol text.
    """
    for pk in ([_cmax(100.0)], []):  # range derivable, and not derivable
        plan = plan_bioanalytical(
            pk_parameters=pk, label=PLAIN_LABEL, drug_inn="testdrug")
        directive = bioanalytical_directive(plan)

        assert "do NOT state any numeric LLOQ" in directive
        assert "Bioanalytical Method Validation report" in directive
        assert "Quality controls:" not in directive

        # The derived numbers must not leak into the prompt in any form.
        cal = plan.get("calibration") or {}
        for key in ("lloq", "uloq"):
            val = cal.get(key)
            if isinstance(val, (int, float)) and val:
                assert f"{val:g} ng/mL" not in directive, (
                    f"{key}={val:g} ng/mL leaked into the protocol directive")
        for q in cal.get("qc_levels") or []:
            conc = q.get("concentration")
            if isinstance(conc, (int, float)) and conc:
                assert f"{conc:g} ng/mL" not in directive, (
                    f"QC {conc:g} ng/mL leaked into the protocol directive")


def test_summary_reads_as_protocol_prose():
    plan = plan_bioanalytical(
        pk_parameters=[_cmax(100.0)], label=PLAIN_LABEL, drug_inn="testdrug")
    summary = plan["summary"]
    assert "calibration" in summary.lower() or "validated over" in summary.lower()
    assert "incurred sample reanalysis" in summary.lower()
    assert len(summary) > 200
