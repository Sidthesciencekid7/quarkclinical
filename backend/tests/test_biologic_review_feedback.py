"""Regression tests for reviewer feedback on the canakinumab protocol.

A clinical pharmacologist reviewed a real generated biosimilar protocol and
found five defects. Each is pinned here so it cannot silently return:

1. CYP/transporter comedication rules imposed on a monoclonal antibody.
2. A numeric bioanalytical LLOQ asserted in the protocol (out of scope - it is
   the analytical laboratory's to set at method validation).
3. Hy's Law and the FDA DILI liver thresholds applied to a biologic.
4. Subjects not required to be naive to the biologic.
5. QC FAILs listed in an appendix while the document exported anyway.
"""
from __future__ import annotations

import json

import pytest

from services.bioanalytical import bioanalytical_directive, plan_bioanalytical
from services.ddi_screening import ddi_directive, screen_interactions
from services.safety_gating import recommend_safety_gating, safety_gating_directive

BIOLOGIC_LABEL = {
    "clinical_pharmacology": (
        "Canakinumab is a human monoclonal antibody. It is not metabolised by "
        "cytochrome P450 enzymes."
    ),
    "dosage_and_administration": "For subcutaneous injection.",
    "drug_interactions": "No formal drug interaction studies have been conducted.",
}
SMALL_MOLECULE_LABEL = {
    "clinical_pharmacology": "Metoprolol is metabolized predominantly by CYP2D6.",
    "dosage_and_administration": "Oral tablet.",
}


def _biologic_ddi():
    return screen_interactions(
        BIOLOGIC_LABEL, None, "canakinumab", design_key="parallel", is_biologic=True)


# ---------------------------------------------------------------------------
# 1. No CYP / transporter rules for a therapeutic protein
# ---------------------------------------------------------------------------


def test_biologic_gets_no_cyp_or_transporter_rule():
    r = _biologic_ddi()
    rows = r["prohibited"] + r["restricted"]
    for row in rows:
        blob = json.dumps(row).lower()
        for banned in ("cyp", "p-gp", "oatp", "bcrp", "transporter", "grapefruit"):
            assert banned not in blob, f"{row['key']} mentions {banned!r}"
    assert r["pathway_keys"] == []
    assert r["modality"] == "biologic"


def test_biologic_never_gets_the_pan_pathway_fallback():
    """The old fallback printed 'inhibitors or inducers of any major CYP enzyme'
    when no pathway was found - actively wrong for a molecule with no CYP
    involvement at all."""
    keys = {row["key"] for row in _biologic_ddi()["prohibited"]}
    assert "generic_perpetrators" not in keys


def test_small_molecule_still_gets_its_pathway_rules():
    """The biologic branch must not disable screening for everything else."""
    r = screen_interactions(
        SMALL_MOLECULE_LABEL, None, "metoprolol", design_key="crossover",
        is_biologic=False)
    assert r.get("modality") != "biologic"
    assert r["pathway_keys"], "CYP2D6 should still be detected for metoprolol"


def test_biologic_directive_forbids_cyp_clauses_and_grapefruit():
    d = ddi_directive(_biologic_ddi())
    assert "NOT a cytochrome-P450 substrate" in d
    assert "grapefruit" in d.lower()  # named only to FORBID it
    assert "must NOT restrict grapefruit" in d


# ---------------------------------------------------------------------------
# 2. Naive-to-biologic exclusion
# ---------------------------------------------------------------------------


def test_biologic_requires_naive_subjects():
    r = _biologic_ddi()
    assert r["requires_naive_subjects"] is True
    row = next(x for x in r["prohibited"] if x["key"] == "prior_exposure_naive")
    assert row["washout_days"] is None, "prior exposure is permanent, not a washout"
    assert "anti-drug antibod" in row["rationale"].lower()


def test_naive_requirement_reaches_the_prompt():
    d = ddi_directive(_biologic_ddi())
    assert "BIOLOGIC-NAIVE" in d
    assert "baseline anti-drug antibody" in d
    assert "NOT as a washout period" in d


# ---------------------------------------------------------------------------
# 3. Bioanalytical LLOQ is out of scope for the protocol
# ---------------------------------------------------------------------------


def test_protocol_directive_forbids_any_numeric_lloq():
    plan = plan_bioanalytical(
        pk_parameters=[{"parameter": "Cmax", "value": 16.0, "unit": "ug/mL",
                        "source_pmid": "1"}],
        label=BIOLOGIC_LABEL, drug_inn="canakinumab",
        study_type="biosimilar_pk", is_biologic=True,
    )
    d = bioanalytical_directive(plan)
    assert "do NOT state any numeric LLOQ" in d
    assert "Bioanalytical Method Validation report" in d
    # The concrete range must not be handed to the model at all.
    assert "Calibration range: LLOQ" not in d


# ---------------------------------------------------------------------------
# 4. Hepatic + QT gating are small-molecule mechanisms
# ---------------------------------------------------------------------------


def _bio_gating(**kw):
    return recommend_safety_gating(
        study_type="biosimilar_pk", design_key="parallel", n_enrolled=94,
        label=BIOLOGIC_LABEL, is_biologic=True, **kw)


def test_hys_law_and_dili_thresholds_dropped_for_biologics():
    keys = {r["key"] for r in
            _bio_gating()["individual_rules"] + _bio_gating()["cohort_rules"]}
    for k in ("ind_hys_law", "coh_hys_law", "ind_hepatic_discontinuation"):
        assert k not in keys, f"{k} is a small-molecule DILI rule"


def test_hys_law_retained_for_small_molecules():
    r = recommend_safety_gating(
        study_type="BE", design_key="crossover", n_enrolled=24,
        label=SMALL_MOLECULE_LABEL, is_biologic=False)
    keys = {x["key"] for x in r["individual_rules"] + r["cohort_rules"]}
    assert "ind_hys_law" in keys, "removing DILI gating for small molecules is unsafe"


def test_qt_gating_dropped_for_biologics_unless_the_label_says_otherwise():
    """hERG block is a small-molecule mechanism; an antibody cannot enter the pore."""
    keys = {r["key"] for r in _bio_gating()["individual_rules"]}
    assert "ind_qt_prolongation" not in keys

    flagged = recommend_safety_gating(
        study_type="biosimilar_pk", design_key="parallel", n_enrolled=94,
        label={**BIOLOGIC_LABEL, "warnings": "QT prolongation has been observed."},
        is_biologic=True,
    )
    assert any(r["key"] == "ind_qt_prolongation" for r in flagged["individual_rules"]), \
        "a real label QT signal must still be gated"


def test_biologic_suppression_is_disclosed_not_silent():
    esc = {e["key"] for e in _bio_gating()["escalations"]}
    assert "biologic_modality" in esc


def test_anaphylaxis_and_injection_site_survive_the_trim():
    """The trim must not remove the rules that actually matter for a SC biologic."""
    keys = {r["key"] for r in
            _bio_gating()["individual_rules"] + _bio_gating()["cohort_rules"]}
    assert "ind_anaphylaxis" in keys
    assert "ind_injection_site_reaction" in keys


# ---------------------------------------------------------------------------
# 5. The safety section must be materially shorter
# ---------------------------------------------------------------------------


def test_safety_directive_drops_per_rule_rationale():
    """Rationale is the engine explaining its own choice - provenance for the
    author, not protocol content. 19 of them made the section unreadable."""
    d = safety_gating_directive(_bio_gating())
    assert "Rationale:" not in d
    assert "included because:" not in d


def test_safety_directive_asks_for_a_table_not_prose():
    d = safety_gating_directive(_bio_gating())
    assert "COMPACT HTML" in d
    assert "do NOT add a rationale column" in d


def test_directives_are_ascii_safe():
    """Windows cp1252 console: a non-ASCII char in a directive has crashed
    generation logging before."""
    for d in (ddi_directive(_biologic_ddi()),
              safety_gating_directive(_bio_gating())):
        assert d.isascii(), "directive contains non-ASCII"


def test_engine_results_round_trip_as_jsonb():
    for r in (_biologic_ddi(), _bio_gating()):
        assert json.loads(json.dumps(r))


# ---------------------------------------------------------------------------
# Reviewer finding: "hys law etc not applicable to biologics" - suppressing the
# RULE is not enough. The boundary also appeared in the overview paragraph, in
# the discharge checklist and in the disclosed assumptions, so the protocol
# still discussed a threshold it did not have. These pin every surface.
# ---------------------------------------------------------------------------


def _sm_gating():
    """Small-molecule oral BE comparator - the rules must SURVIVE here."""
    return recommend_safety_gating(
        study_type="BE", design_key="crossover", n_enrolled=24,
        label={"dosage_and_administration": "50 mg tablet taken orally"},
        is_biologic=False, route="oral", healthy_volunteers=True,
    )


def test_biologic_summary_never_names_a_qt_or_hys_law_boundary():
    """The overview paragraph is the one part every reader reads. Naming a
    boundary the study does not have puts back exactly the dead text the
    suppression removed."""
    summary = _bio_gating()["summary"]
    assert "QTc" not in summary
    assert "Hy's Law" not in summary
    assert "bilirubin" not in summary.lower()


def test_biologic_has_no_qtcf_discharge_gate():
    """A bedside discharge check with no stopping rule behind it is orphaned."""
    keys = [c["key"] for c in _bio_gating()["discharge_criteria"]["criteria"]]
    assert "ecg" not in keys
    assert "ecg" in [c["key"] for c in _sm_gating()["discharge_criteria"]["criteria"]], (
        "the ECG discharge gate must still apply to a small molecule")


def test_biologic_does_not_disclose_a_qt_halting_assumption():
    """Disclosing the convention behind a rule that was removed re-creates the
    dead text in the assumptions list."""
    bio_keys = {a["key"] for a in _bio_gating()["assumptions"]}
    assert "qt_halt_subject_count" not in bio_keys
    assert "qt_halt_subject_count" in {a["key"] for a in _sm_gating()["assumptions"]}


def test_suppressed_rules_are_reported_not_silently_dropped():
    """A reviewer must be able to see that Hy's Law was CONSIDERED and
    deliberately not applied, rather than simply missing."""
    bio = _bio_gating()
    suppressed = set(bio.get("suppressed_rules") or ())
    assert {"ind_hys_law", "coh_hys_law", "ind_qt_prolongation"} <= suppressed
    assert not (set(_sm_gating().get("suppressed_rules") or ()))
    # and the reason is stated in the generated text, not just in metadata
    assert "Hy's Law" in safety_gating_directive(bio), (
        "the directive should explain that Hy's Law was deliberately NOT applied")


def test_small_molecule_keeps_hys_law_and_qt():
    """The suppression must be biologic-specific, not a global deletion."""
    d = safety_gating_directive(_sm_gating())
    assert "Hy's Law" in d and "QTcF" in d


# ---------------------------------------------------------------------------
# 6. The modality gate never reached recommend_stratification() in production
# ---------------------------------------------------------------------------
# routers/protocols.py::_augment_config_with_design() computes `is_biologic`
# and passes it to every OTHER deterministic engine call in that function
# (sampling schedule, DDI screen, safety gating, bioanalytical, CRF spec) but
# the recommend_stratification() call omitted it, so in production it always
# ran with services/stratification.py's is_biologic=False default. Findings
# 1-5 above (the engine-level protections) are real and correctly tested, but
# this router-level wiring gap meant the modality-suppression gate they share
# a pattern with (dropping CYP/transporter/smoking/NAT2/gastric-pH factors,
# and the non-smoker-status eligibility criterion they would otherwise leak)
# never actually fired for a real biosimilar_pk protocol.


def test_stratification_modality_gate_actually_fires_for_a_biologic():
    from routers.protocols import _augment_config_with_design

    cfg = _augment_config_with_design(
        population_config={},
        study_type="biosimilar_pk",
        drug_inn="canakinumab",
        extracted=[],
        pk_params=[],
        label=BIOLOGIC_LABEL,
    )
    strat = cfg.get("_stratification")
    assert strat is not None
    assert strat["is_biologic"] is True, (
        "recommend_stratification() was not told this is a biologic at the "
        "router call site — the modality-suppression gate never fires for a "
        "real biosimilar protocol"
    )
    suppressed_keys = {s["key"] for s in strat["suppressed_factors"]}
    assert "smoking_status" in suppressed_keys
    assert "cyp_polymorphism" in suppressed_keys


def test_stratification_modality_gate_stays_off_for_a_small_molecule():
    from routers.protocols import _augment_config_with_design

    cfg = _augment_config_with_design(
        population_config={},
        study_type="BE",
        drug_inn="metoprolol",
        extracted=[],
        pk_params=[],
        label=SMALL_MOLECULE_LABEL,
    )
    strat = cfg.get("_stratification")
    assert strat is not None
    assert strat["is_biologic"] is False
    assert strat["suppressed_factors"] == []
