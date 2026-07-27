"""Unit tests for services/design_decisions.py — the 5 non-negotiable clinical
study-design rules (population/safety, dose, route/device/site, design,
PD co-primary) and the live-label extraction that grounds the route decision.

Pure-function tests, no DB/API/AI — run standalone: pytest tests/test_design_decisions.py
"""
from __future__ import annotations

from services.design_decisions import (
    allowed_study_types,
    classify_drug_modality,
    determine_design_decisions,
    extract_label_device,
    extract_label_routes,
    extract_label_site,
)


def _by_key(decisions, key):
    return next(d for d in decisions if d.key == key)


# ---------------------------------------------------------------------------
# Label text extraction (route / device / site)
# ---------------------------------------------------------------------------


def test_extract_routes_affirmative():
    assert extract_label_routes("Administer subcutaneously into the abdomen.") == ("SC",)
    assert extract_label_routes("For intravenous infusion only.") == ("IV",)
    assert extract_label_routes("Available as 200 mg tablets, take orally.") == ("oral",)


def test_extract_routes_both_when_both_labelled():
    assert extract_label_routes("May be given subcutaneously or intravenously.") == ("SC", "IV")


def test_extract_routes_excludes_negated_route():
    """'Do not administer intravenously' must NOT be read as IV being available —
    this was a real bug caught during development (naive keyword match picked up
    the negated mention as if it were an affirmative route)."""
    text = "Administer subcutaneously into the abdomen. Do not administer intravenously."
    assert extract_label_routes(text) == ("SC",)


def test_extract_routes_negation_does_not_suppress_other_affirmative_mention():
    """A route negated in one sentence must still count if genuinely offered
    elsewhere in the label (only a route with NO affirmative mention is dropped)."""
    text = "This product should not be given subcutaneously. Administer only by intravenous infusion."
    assert extract_label_routes(text) == ("IV",)


def test_extract_routes_empty_text():
    assert extract_label_routes("") == ()
    assert extract_label_routes(None) == ()  # type: ignore[arg-type]


def test_extract_device_pfs_only():
    assert extract_label_device("Supplied as a pre-filled syringe for single use.") == "PFS"


def test_extract_device_autoinjector_only():
    assert extract_label_device("Available in an auto-injector for self-administration.") == "autoinjector"


def test_extract_device_ambiguous_returns_none():
    """Both devices mentioned (label covers multiple presentations) -> no single
    override; the engine's own PFS-preferred default applies instead."""
    assert extract_label_device("Available as a prefilled syringe or an autoinjector.") is None


def test_extract_site_single_mention():
    assert extract_label_site("Inject into the abdomen, rotating sites with each dose.") == "abdomen"


def test_extract_site_multiple_mentions_returns_none():
    assert extract_label_site("May be injected into the abdomen or thigh.") is None


# ---------------------------------------------------------------------------
# determine_design_decisions — evidence field reflects whether the label was
# actually consulted (vs. the curated-KB / generic fallback)
# ---------------------------------------------------------------------------


def test_route_decision_uses_live_label_when_available():
    decisions = determine_design_decisions(
        drug_inn="an unlisted biologic",
        study_type="biosimilar_pk",
        design_key="parallel",
        t_half_hours=300.0,
        label_text="Administer subcutaneously into the abdomen using a pre-filled syringe.",
    )
    route = _by_key(decisions, "route")
    assert route.value == "SC · prefilled syringe · abdomen"
    assert "Live DailyMed label" in route.evidence


def test_route_decision_falls_back_to_curated_kb_without_label():
    decisions = determine_design_decisions(
        drug_inn="an unlisted biologic",
        study_type="biosimilar_pk",
        design_key="parallel",
        t_half_hours=300.0,
    )
    route = _by_key(decisions, "route")
    assert "Curated knowledge base" in route.evidence


def test_route_decision_label_iv_only_overrides_generic_sc_default():
    decisions = determine_design_decisions(
        drug_inn="an unlisted biologic",
        study_type="biosimilar_pk",
        design_key="parallel",
        t_half_hours=300.0,
        label_text="For intravenous infusion only.",
    )
    route = _by_key(decisions, "route")
    assert route.value == "IV infusion"
    assert "Live DailyMed label" in route.evidence


# ---------------------------------------------------------------------------
# Full-rule archetypes (population/dose/design/PD) — no label involved
# ---------------------------------------------------------------------------


def test_insulin_archetype():
    decisions = determine_design_decisions(
        drug_inn="insulin glargine", study_type="biosimilar_pk",
        design_key="crossover", t_half_hours=5.0,
    )
    assert _by_key(decisions, "population").value == "Healthy volunteers"
    assert _by_key(decisions, "design").value == "Crossover"
    assert "GIR" in _by_key(decisions, "pd_endpoint").value or "Glucose infusion rate" in _by_key(decisions, "pd_endpoint").value


def test_trastuzumab_nhv_exception():
    """Oncology mAb, but safe single-dose in NHV -> NHV allowed despite being oncology."""
    decisions = determine_design_decisions(
        drug_inn="trastuzumab", study_type="biosimilar_pk",
        design_key="parallel", t_half_hours=672.0,
    )
    assert _by_key(decisions, "population").value == "Healthy volunteers"


def test_pembrolizumab_requires_patients():
    """Checkpoint inhibitor with immune toxicity -> patients, not NHV."""
    decisions = determine_design_decisions(
        drug_inn="pembrolizumab", study_type="biosimilar_pk",
        design_key="parallel", t_half_hours=500.0,
    )
    assert _by_key(decisions, "population").value.startswith("Patients")


def test_ibuprofen_oral_no_pd():
    decisions = determine_design_decisions(
        drug_inn="ibuprofen", study_type="BE", design_key="crossover", t_half_hours=2.0,
    )
    assert _by_key(decisions, "route").value == "Oral"
    assert _by_key(decisions, "pd_endpoint").value == "None (PK only)"
    # HbA1c must never be selected as a PK-window PD co-primary (user-confirmed rule).
    assert "HbA1c" not in _by_key(decisions, "pd_endpoint").value


# ---------------------------------------------------------------------------
# Modality classification (biologic vs small molecule) — gates which study
# types are selectable for a given drug (user rule: "if the drug is a
# biologic it can only select the respective study type").
# ---------------------------------------------------------------------------


def test_modality_curated_kb_biologics():
    for inn in ("adalimumab", "trastuzumab", "pembrolizumab", "insulin glargine", "filgrastim"):
        assert classify_drug_modality(inn) == "biologic", inn


def test_modality_curated_kb_small_molecules():
    for inn in ("ibuprofen", "metformin", "atorvastatin", "warfarin"):
        assert classify_drug_modality(inn) == "small_molecule", inn


def test_modality_stem_heuristic_unknown_mab():
    """A mAb not in the curated KB is still caught by the WHO '-mab' INN stem."""
    assert classify_drug_modality("denosumab") == "biologic"


def test_modality_stem_heuristic_unknown_peptide_biologic():
    """GLP-1/peptide analogues ('-tide') have been regulated as biologics since
    the 2020 BPCI Act transition, even though they're not in the curated KB."""
    assert classify_drug_modality("dulaglutide") == "biologic"


def test_modality_stem_heuristic_unknown_small_molecule():
    assert classify_drug_modality("imaginib") == "small_molecule"
    assert classify_drug_modality("fakeprofen") == "small_molecule"


def test_modality_unclassifiable_returns_unknown():
    """A made-up name matching no KB entry or INN stem must NOT be force-fit
    into either category — the caller should leave the choice unrestricted."""
    assert classify_drug_modality("xyzzyplorb") == "unknown"


def test_allowed_study_types_biologic_only_biosimilar_pk():
    assert allowed_study_types("adalimumab") == ("biosimilar_pk",)


def test_allowed_study_types_small_molecule_excludes_biosimilar_pk():
    allowed = allowed_study_types("ibuprofen")
    assert allowed == ("BE", "food_effect")
    assert "biosimilar_pk" not in allowed


def test_allowed_study_types_unknown_drug_is_unrestricted():
    assert allowed_study_types("xyzzyplorb") is None
