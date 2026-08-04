"""Tests for the PK-variability stratification engine.

The engine is a deterministic knowledge layer: given a design and the real label
text, it must return a stable, defensible factor list. These tests pin the two
things most likely to regress — the *design-aware* framing (a crossover must not
claim a precision benefit it does not have) and the label-driven applicability.
"""
from __future__ import annotations

import json

from services.stratification import (
    _CATALOGUE,
    _SMALL_MOLECULE_ONLY,
    recommend_stratification,
    stratification_directive,
)

# A small-molecule-flavoured label that positively cues EVERY metabolism-driven
# factor. Both modality branches are run against this same text, so the only
# difference between them is the is_biologic flag.
_CUEING_LABEL = {
    "clinical_pharmacology": (
        "Cleared primarily by CYP2D6 with a contribution from CYP1A2; poor "
        "metabolizers show markedly higher exposure. The drug is a substrate of "
        "the OATP1B1 uptake transporter and of P-glycoprotein, and a portion of "
        "the dose is cleared by N-acetylation (NAT2). Absorption is pH-dependent "
        "and is reduced by proton pump inhibitor use. Smoking increases clearance."
    ),
}

# A real-shaped monoclonal-antibody label: it cues NOTHING in the catalogue,
# which is the case the modality prohibition has to survive.
_QUIET_BIOLOGIC_LABEL = {
    "clinical_pharmacology": (
        "Canakinumab is a human monoclonal antibody that binds interleukin-1 beta. "
        "It is expected to be degraded by proteolytic catabolism into small "
        "peptides and amino acids. Absolute bioavailability following "
        "subcutaneous administration is approximately 70 percent."
    ),
}

_METABOLISM_DRIVEN = (
    "cyp_polymorphism",
    "transporter_polymorphism",
    "nat2_acetylator",
    "smoking_status",
    "gastric_ph",
    "ancestry",
)


# ---------------------------------------------------------------------------
# Catalogue integrity
# ---------------------------------------------------------------------------


def test_catalogue_is_well_formed():
    keys = [f.key for f in _CATALOGUE]
    assert len(keys) == len(set(keys)), "duplicate factor keys"
    for f in _CATALOGUE:
        assert f.tier in (1, 2, 3), f"{f.key} has an out-of-range tier"
        assert f.label and f.magnitude and f.rationale and f.evidence
        assert f.applies_when
        # Rationale must be a real explanation, not a stub.
        assert len(f.rationale) > 80, f"{f.key} rationale too thin to justify a row"


# ---------------------------------------------------------------------------
# Universal factors
# ---------------------------------------------------------------------------


def test_sex_and_body_size_always_recommended():
    """These are universal at a fixed dose and cost nothing to balance."""
    out = recommend_stratification(design_key="crossover", label=None)
    recommended = {f["key"] for f in out["factors"]}
    assert "sex" in recommended
    assert "body_size" in recommended


def test_no_label_does_not_crash_and_still_returns_factors():
    out = recommend_stratification(design_key="parallel", label=None)
    assert out["factors"], "should still return the universal factors"
    assert out["summary"]


# ---------------------------------------------------------------------------
# Label-driven applicability
# ---------------------------------------------------------------------------


def test_cyp2d6_label_triggers_polymorphism_and_ancestry():
    label = {
        "clinical_pharmacology": (
            "Metoprolol is metabolized primarily by CYP2D6. In poor metabolizers, "
            "plasma concentrations are several-fold higher."
        )
    }
    out = recommend_stratification(design_key="parallel", label=label)
    keys = {f["key"] for f in out["factors"]}
    assert "cyp_polymorphism" in keys
    # Ancestry rides along, because it is a proxy for allele frequency.
    assert "ancestry" in keys


def test_renal_label_triggers_renal_function():
    label = {"pharmacokinetics": "Approximately 90% of the dose is excreted unchanged in the urine."}
    out = recommend_stratification(design_key="parallel", label=label)
    assert "renal_function" in {f["key"] for f in out["factors"]}


def test_unrelated_label_does_not_trigger_genotype_factors():
    label = {"pharmacokinetics": "Absorption is rapid and the drug is widely distributed."}
    out = recommend_stratification(design_key="crossover", label=label)
    keys = {f["key"] for f in out["factors"]}
    assert "cyp_polymorphism" not in keys
    assert "nat2_acetylator" not in keys


# ---------------------------------------------------------------------------
# The design nuance — the clinically important part
# ---------------------------------------------------------------------------


def test_parallel_is_between_subject_relevant_but_crossover_is_not():
    par = recommend_stratification(design_key="parallel", label=None)
    xo = recommend_stratification(design_key="crossover", label=None)
    assert par["between_subject_relevant"] is True
    assert xo["between_subject_relevant"] is False


def test_crossover_effect_wording_does_not_overclaim_precision():
    """A crossover subject is their own control — stratifying must NOT be sold
    as narrowing the confidence interval."""
    xo = recommend_stratification(design_key="crossover", label=None)
    for f in xo["factors"]:
        effect = f["effect"].lower()
        assert "sequence" in effect, "crossover benefit is sequence balance"
        assert "confidence interval" not in effect, (
            f"{f['key']}: crossover must not claim a CI benefit")

    par = recommend_stratification(design_key="parallel", label=None)
    assert any("confidence interval" in f["effect"].lower() for f in par["factors"]), (
        "parallel design genuinely tightens the between-subject error term")


def test_summary_reflects_design():
    xo = recommend_stratification(design_key="crossover", label=None)["summary"].lower()
    par = recommend_stratification(design_key="parallel", label=None)["summary"].lower()
    assert "own control" in xo
    assert "between subject" in par


# ---------------------------------------------------------------------------
# Ordering, eligibility honesty, serialisation
# ---------------------------------------------------------------------------


def test_factors_are_tier_ordered():
    label = {"clinical_pharmacology": "Cleared by CYP2C19; also renally excreted unchanged in urine."}
    out = recommend_stratification(design_key="parallel", label=label)
    tiers = [f["tier"] for f in out["factors"]]
    assert tiers == sorted(tiers), "highest-impact factors must come first"


def test_eligibility_narrowing_is_flagged_honestly():
    """A healthy-volunteer BE protocol already restricts renal function; the
    engine must say so rather than promising a redundant stratification."""
    label = {"pharmacokinetics": "Excreted unchanged in the urine; renal impairment increases exposure."}
    out = recommend_stratification(
        design_key="parallel", label=label, healthy_volunteers=True)
    renal = next(f for f in out["factors"] if f["key"] == "renal_function")
    assert renal["narrowed_by_eligibility"] is True
    assert "normal renal" in renal["note"].lower()


def test_nti_and_hvd_raise_the_stakes_in_summary():
    nti = recommend_stratification(design_key="replicate", label=None, is_nti=True)
    assert "narrow-therapeutic-index" in nti["summary"].lower()

    hvd = recommend_stratification(design_key="replicate", label=None, cv_intra_pct=42.0)
    assert "highly" in hvd["summary"].lower() and "42.0" in hvd["summary"]


def test_result_is_json_serialisable_for_population_config():
    """It is persisted inside population_config (JSONB), so it must round-trip."""
    out = recommend_stratification(design_key="parallel", label={"pharmacokinetics": "CYP2D6"})
    restored = json.loads(json.dumps(out))
    assert restored["factors"][0]["key"] == out["factors"][0]["key"]


def test_directive_locks_the_factors_for_the_prompt():
    out = recommend_stratification(design_key="parallel", label=None)
    directive = stratification_directive(out)
    assert "ALREADY DETERMINED" in directive
    for f in out["factors"]:
        assert f["label"] in directive
    # Empty input yields no directive rather than a misleading empty header.
    assert stratification_directive({}) == ""


# ---------------------------------------------------------------------------
# Modality gate — a therapeutic protein is not a small molecule
#
# The reviewing clinical pharmacologist's finding on a real canakinumab
# protocol: "subject restrictions are too much for a biologic", "make the
# criteria only as it is applicable to the product". A monoclonal antibody is
# catabolised to amino acids by proteolysis; it is not a CYP substrate, not a
# transporter substrate, not acetylated, and never swallowed. Carrying those
# factors imports real eligibility criteria (non-smoker status, genotype
# screening, acid-suppressant washout) that cost recruitable volunteers for a
# mechanism the product does not have.
# ---------------------------------------------------------------------------


def _bio(label=_CUEING_LABEL, design_key="parallel"):
    return recommend_stratification(
        design_key=design_key, label=label, is_biologic=True)


def _small_molecule(label=_CUEING_LABEL, design_key="parallel"):
    return recommend_stratification(design_key=design_key, label=label)


def test_biologic_suppresses_metabolism_driven_factors():
    out = _bio()
    keys = {f["key"] for f in out["factors"]}
    for key in _METABOLISM_DRIVEN:
        assert key not in keys, f"{key} has no mechanism in a therapeutic protein"


def test_small_molecule_keeps_metabolism_driven_factors():
    """The gate must be modality-specific, not a blanket deletion."""
    out = _small_molecule()
    keys = {f["key"] for f in out["factors"]}
    for key in _METABOLISM_DRIVEN:
        assert key in keys, f"{key} is genuinely in play for a small molecule"
    assert out["suppressed_factors"] == []
    assert out["is_biologic"] is False


def test_body_size_and_sex_survive_in_both_modalities():
    """Exposure scales with body mass at a fixed dose for a protein too — this
    is the over-suppression guard."""
    for out in (_bio(), _small_molecule()):
        keys = {f["key"] for f in out["factors"]}
        assert "sex" in keys
        assert "body_size" in keys


def test_biologic_suppressed_factors_are_disclosed_with_a_mechanism():
    out = _bio()
    disclosed = {s["key"]: s for s in out["suppressed_factors"]}
    assert set(disclosed) == set(_SMALL_MOLECULE_ONLY)
    for key, entry in disclosed.items():
        assert entry["label"], f"{key} disclosed without a label"
        assert "Not applicable" in entry["reason"]
        # This label cued every one of them, so the gate really did prevent a row.
        assert entry["would_have_been_recommended"] is True


def test_biologic_prohibition_survives_a_label_that_cues_nothing():
    """The regression this pins: a real mAb label names no CYP, no transporter
    and no acid suppressant, so a cue-conditioned suppression list would be
    EMPTY for exactly the product the gate exists for."""
    out = _bio(label=_QUIET_BIOLOGIC_LABEL)
    disclosed = {s["key"]: s for s in out["suppressed_factors"]}
    assert set(disclosed) == set(_SMALL_MOLECULE_ONLY)
    assert all(s["would_have_been_recommended"] is False for s in disclosed.values())
    directive = stratification_directive(out)
    assert "MODALITY - THERAPEUTIC PROTEIN" in directive
    assert "non-smoker" in directive


def test_suppressed_factor_text_does_not_survive_anywhere_in_the_result():
    """Suppressing the row but leaving its rationale in `considered` would put
    the CYP1A2 argument and the 'Inclusion requires non-smoker status' note back
    into the context the prompt is built from."""
    out = _bio()
    considered = {f["key"] for f in out["considered"]}
    for key in _METABOLISM_DRIVEN:
        assert key not in considered
    body = json.dumps(
        {"factors": out["factors"], "considered": out["considered"],
         "summary": out["summary"]}
    ).lower()
    for banned in ("cyp", "smok", "genotype", "metaboli", "acetylat", "transporter"):
        assert banned not in body, (
            f"'{banned}' reached the biologic factor text; a protein protocol "
            "must not argue from small-molecule metabolism")


def test_biologic_retained_factors_argue_from_a_protein_mechanism():
    """Sex survives, but its small-molecule rationale (CYP3A4/CYP1A2 activity)
    must not: for a protein the sex effect is body composition."""
    sex = next(f for f in _bio()["factors"] if f["key"] == "sex")
    assert "body-composition" in sex["rationale"] or "body mass" in sex["rationale"]

    sm_sex = next(f for f in _small_molecule()["factors"] if f["key"] == "sex")
    assert "CYP3A4" in sm_sex["rationale"], (
        "the small-molecule rationale must be left untouched")


def test_biologic_summary_flags_the_exclusion_without_naming_it():
    """The summary IS protocol prose. Naming smoking status there would put the
    non-smoker question straight back into the document."""
    summary = _bio(label=_QUIET_BIOLOGIC_LABEL)["summary"]
    assert "therapeutic protein" in summary
    assert "considered and excluded" in summary
    assert "smok" not in summary.lower()


def test_biologic_directive_forbids_reintroducing_a_suppressed_factor():
    out = _bio()
    directive = stratification_directive(out)
    assert "MODALITY - THERAPEUTIC PROTEIN" in directive
    for s in out["suppressed_factors"]:
        assert f"EXCLUDED: {s['label']}" in directive
        assert s["reason"] in directive
    # The concrete leaks, named so the model cannot arrive at them sideways.
    lowered = directive.lower()
    for phrase in ("non-smoker", "genotype", "proton pump", "race or ethnicity"):
        assert phrase in lowered
    assert "do not add a stratification factor" in lowered


def test_biologic_directive_does_not_list_a_suppressed_factor_as_a_factor():
    """The prohibition and the table must not contradict each other."""
    out = _bio()
    directive = stratification_directive(out)
    factor_block = directive.split("Factors:", 1)[1]
    for s in out["suppressed_factors"]:
        assert s["label"] not in factor_block
    for f in out["factors"]:
        assert f["label"] in factor_block


def test_small_molecule_directive_carries_no_modality_block():
    directive = stratification_directive(_small_molecule())
    assert "MODALITY" not in directive
    assert "EXCLUDED" not in directive


def test_biologic_result_is_json_serialisable_for_population_config():
    """population_config is a JSONB column — the suppression must round-trip."""
    out = _bio()
    restored = json.loads(json.dumps(out))
    assert restored == out
    assert restored["is_biologic"] is True
    assert restored["suppressed_factors"][0]["would_have_been_recommended"] is True

    quiet = _bio(label=_QUIET_BIOLOGIC_LABEL, design_key="crossover")
    assert json.loads(json.dumps(quiet)) == quiet


def test_biologic_flag_defaults_to_small_molecule_behaviour():
    """Callers that never pass the flag must be completely unaffected."""
    assert (recommend_stratification(design_key="parallel", label=_CUEING_LABEL)
            == recommend_stratification(design_key="parallel", label=_CUEING_LABEL,
                                        is_biologic=False))
