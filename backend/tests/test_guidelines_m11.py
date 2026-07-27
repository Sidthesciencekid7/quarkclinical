"""Unit tests for the ICH M11 guideline entry (F1) in services/guidelines.py.

M11 is the harmonised clinical-trial-protocol TEMPLATE standard (CeSHarP), added
as a selectable guideline whose directive nudges M11-aligned section structure /
terminology. The bioequivalence acceptance methodology itself stays per ICH M13.

Pure-function tests, no DB/API/AI - run standalone: pytest tests/test_guidelines_m11.py
"""
from __future__ import annotations

from services.guidelines import (
    GUIDELINES,
    get_profile,
    is_current,
    is_supported,
    regulatory_directive,
)

M11_ID = "ICH-M11-2025"


def test_m11_registered():
    assert M11_ID in GUIDELINES


def test_m11_is_supported():
    assert is_supported(M11_ID) is True


def test_m11_regulator_is_ich():
    assert get_profile(M11_ID).regulator == "ICH"


def test_m11_profile_fields():
    p = get_profile(M11_ID)
    assert p.id == M11_ID
    assert p.display_name == "ICH M11 - Harmonised Protocol Template (2025)"
    assert p.region == "International (ICH)"
    assert p.is_current is True


def test_m11_in_selectable_current_list():
    # Mirrors the router's selectable-list logic (is_current only), without
    # importing FastAPI/DB.
    current_ids = {p.id for p in GUIDELINES.values() if p.is_current}
    assert M11_ID in current_ids
    assert is_current(M11_ID) is True


def test_m11_registry_stays_ascii():
    # The module mandates ASCII-only strings (no em/en-dashes).
    p = get_profile(M11_ID)
    for field in (
        p.display_name, p.regulator, p.region, p.citation,
        p.reference_product_term, p.acceptance_summary, p.hvd_approach,
        p.nti_approach, p.food_convention, p.structural_note,
    ):
        assert field.isascii(), field


def test_m11_directive_appends_structural_note():
    # M11's whole point is protocol structure: its structural_note must surface
    # in the generation directive.
    assert get_profile(M11_ID).structural_note != ""
    directive = regulatory_directive(M11_ID, "BE")
    assert "Protocol structure:" in directive
    assert "ICH M11" in directive


def test_non_m11_guideline_has_no_structural_note_line():
    # Other guidelines keep an empty structural_note, so the directive is
    # unchanged for them.
    assert get_profile("ICH-M13A-2024").structural_note == ""
    directive = regulatory_directive("ICH-M13A-2024", "BE")
    assert "Protocol structure:" not in directive
