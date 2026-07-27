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
    recommend_stratification,
    stratification_directive,
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
