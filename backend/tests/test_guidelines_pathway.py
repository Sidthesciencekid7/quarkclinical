"""Submission-pathway framing in services/guidelines.py.

A biosimilar and a generic are NOT the same regulatory submission, even to the
same regulator. In the US a generic small molecule is an ANDA under FD&C Act
505(j) and its comparator is the Reference Listed Drug (RLD); a biosimilar is a
351(k) application under the Public Health Service Act (BPCIA) and its
comparator is the US-licensed reference product. "RLD" does not exist on the
351(k) pathway at all.

Before this, the FDA profile was applied verbatim to every study type, so a
biosimilar protocol was told in every single section prompt that its governing
guidance was the ANDA BE guidance and its comparator an "RLD" - both wrong for a
biologic, and contradicting the FDA-351(k) system prompt the same call already
used (services/ai_service._expertise_for).

Also pinned here: 351(k)/ANDA are US statutes and must never be asserted for an
EMA- or ICH-selected guideline.

Pure-function tests, no DB/AI - run standalone: pytest tests/test_guidelines_pathway.py

Note on the negative assertions: they are deliberately CASE-SENSITIVE. A
lowercased "anda" substring occurs innocently inside ordinary words such as
"standard"/"standardised", so a case-insensitive check would false-positive.
"""
from __future__ import annotations

import pytest

from services.guidelines import (
    GUIDELINES,
    get_profile,
    governing_citation,
    guideline_display_name,
    is_biosimilar_study,
    reference_product_term,
    regulatory_directive,
    submission_pathway,
)

FDA = "FDA-BE-2014"
FDA_OLD = "FDA-BE-2003"
EMA = "EMA-BE-2010"
M13A = "ICH-M13A-2024"
M11 = "ICH-M11-2025"

SMALL_MOLECULE_TYPES = ("BE", "food_effect")


def _facts(directive: str) -> str:
    """Only the '- ' fact lines of a directive.

    The non-biosimilar HEADER deliberately contains the string "FDA/ANDA" - it is
    the standing instruction *not* to default to that language unless the
    guideline really is FDA - so assertions about what a directive ASSERTS must
    look at the fact lines, not the header.
    """
    return "\n".join(ln for ln in directive.splitlines() if ln.startswith("- "))


# ---------------------------------------------------------------------------
# FDA biosimilar -> 351(k) / BPCIA, US-licensed reference product
# ---------------------------------------------------------------------------


def test_fda_biosimilar_directive_names_351k_and_bpcia():
    d = regulatory_directive(FDA, "biosimilar_pk")
    assert "- Submission pathway:" in d
    assert "351(k)" in d
    assert "Biologics Price Competition and Innovation Act" in d
    assert "BPCIA" in d
    assert "Public Health Service Act" in d


def test_fda_biosimilar_comparator_is_the_eu_reference_product_with_a_bridge():
    """PROGRAMME POLICY: the comparator is the EU reference medicinal product
    for every biosimilar study in this application, including those written to
    the FDA 351(k) pathway (a single EU-sourced comparator lot supports both an
    EU and a US filing).

    The comparator term and its scientific bridge are asserted TOGETHER on
    purpose. 351(k) requires comparison against the US-LICENSED reference
    product, so naming an EU-approved comparator WITHOUT the three-way
    analytical/PK bridge that justifies its relevance would be a regulatory
    error, not merely an omission. This test therefore pins both halves — if a
    future change ever drops the bridge while keeping the EU comparator, this
    fails rather than silently shipping an unjustifiable comparator.
    """
    d = regulatory_directive(FDA, "biosimilar_pk")
    assert reference_product_term(FDA, "biosimilar_pk") == (
        "EU reference medicinal product (EU-approved comparator)"
    )
    assert "EU reference medicinal product" in d
    # The bridge, and the US-licensed product it bridges TO, must both be stated.
    assert "SCIENTIFIC BRIDGE" in d
    assert "US-licensed reference product" in d
    assert "three-way" in d.lower()
    # ...and the 351(k) pathway itself is still the governing one (B7).
    assert "351(k)" in d


def test_fda_biosimilar_comparator_bridge_is_absent_for_small_molecules():
    """The bridge is a biosimilar-comparator concept; a small-molecule BE
    directive must not acquire it."""
    d = regulatory_directive(FDA, "BE")
    assert "SCIENTIFIC BRIDGE" not in d
    assert "EU reference medicinal product" not in d
    assert "Reference Listed Drug" in d


def test_ema_biosimilar_comparator_needs_no_cross_region_bridge():
    """An EU filing uses the EU reference product natively, so its statement is
    a sourcing/documentation requirement, not a cross-region bridge to another
    regulator's product."""
    d = regulatory_directive(EMA, "biosimilar_pk")
    assert "European Economic Area" in d
    assert "SCIENTIFIC BRIDGE" not in d
    assert "351(k)" not in d


def test_fda_biosimilar_directive_has_no_generic_drug_terminology():
    """The core defect: ANDA/RLD wording must not reach a biosimilar prompt.

    This covers the FDA food_convention too, which is phrased in RLD terms
    ("where indicated by the RLD labeling") and is therefore excluded from the
    biosimilar directive along with the small-molecule HVD/NTI approaches.
    """
    d = regulatory_directive(FDA, "biosimilar_pk")
    assert "ANDA" not in d
    assert "Reference Listed Drug" not in d
    assert "RLD" not in d
    assert "505(j)" not in d
    assert "Highly variable drugs:" not in d
    assert "Narrow therapeutic index:" not in d


def test_fda_biosimilar_citation_is_the_biosimilar_framework():
    c = governing_citation(FDA, "biosimilar_pk")
    assert "Biosimilarity" in c            # Scientific Considerations ... (2015)
    assert "Immunogenicity" in c           # immunogenicity testing guidance (2019)
    assert "Comparative Analytical" in c   # comparative analytical assessment (2019)
    assert "351(k)" in c
    assert "ANDA" not in c
    assert "21 CFR Part 320" not in c      # generic-drug BE regulation, not 351(k)
    assert c != get_profile(FDA).citation


def test_superseded_fda_profile_uses_the_same_351k_framing():
    """Superseding a small-molecule BE guidance does not change which statute a
    BIOLOGIC is filed under, so a legacy protocol locked to FDA-BE-2003 still
    gets 351(k) rather than the 2003 guidance's ANDA framing."""
    d = regulatory_directive(FDA_OLD, "biosimilar_pk")
    assert "351(k)" in d
    assert "US-licensed reference product" in d
    assert "ANDA" not in d
    assert "RLD" not in d


# ---------------------------------------------------------------------------
# Small molecule (BE / food_effect) - NON-REGRESSION
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("study_type", SMALL_MOLECULE_TYPES)
def test_small_molecule_directive_keeps_anda_and_rld(study_type):
    d = regulatory_directive(FDA, study_type)
    assert "ANDA" in d
    assert "Reference Listed Drug (RLD), US-sourced" in d
    assert "21 CFR Part 320" in d
    assert "351(k)" not in d
    assert "BPCIA" not in d


@pytest.mark.parametrize("study_type", SMALL_MOLECULE_TYPES)
def test_small_molecule_directive_names_the_anda_pathway(study_type):
    d = regulatory_directive(FDA, study_type)
    assert "- Submission pathway: FDA Abbreviated New Drug Application (ANDA)" in d
    assert "505(j)" in d


@pytest.mark.parametrize("study_type", SMALL_MOLECULE_TYPES)
def test_small_molecule_directive_lines_unchanged(study_type):
    """Every line the small-molecule directive carried before the pathway work
    is still there, in the same order; only the pathway line was added."""
    d = regulatory_directive(FDA, study_type)
    p = get_profile(FDA)
    expected_order = [
        "REGULATORY FRAMEWORK - write the protocol to satisfy THIS guideline; use its",
        "terminology and cite it (do not default to FDA/ANDA language unless this IS FDA):",
        f"- Governing guideline: {p.citation}",
        f"- Regulator / region: {p.regulator} ({p.region})",
        f"- Reference/comparator product: refer to it as the {p.reference_product_term}",
        f"- Bioequivalence acceptance criteria: {p.acceptance_summary}",
        f"- Food conditions: {p.food_convention}",
        f"- Highly variable drugs: {p.hvd_approach}",
        f"- Narrow therapeutic index: {p.nti_approach}",
    ]
    last = -1
    for fragment in expected_order:
        idx = d.find(fragment)
        assert idx != -1, f"missing from directive: {fragment}"
        assert idx > last, f"out of order: {fragment}"
        last = idx


def test_small_molecule_biostat_framing_still_present_for_all_guidelines():
    for gid in GUIDELINES:
        d = regulatory_directive(gid, "BE")
        assert "Highly variable drugs:" in d
        assert "Narrow therapeutic index:" in d
        assert "Bioequivalence acceptance criteria:" in d


# ---------------------------------------------------------------------------
# Non-FDA regulators must NOT be given a US statutory pathway
# ---------------------------------------------------------------------------


def test_ema_biosimilar_does_not_assert_fda_351k():
    d = regulatory_directive(EMA, "biosimilar_pk")
    assert "351(k)" not in d
    assert "BPCIA" not in d
    assert "Public Health Service Act" not in d
    assert "ANDA" not in d
    assert "RLD" not in d


def test_ema_biosimilar_uses_the_eu_biosimilar_framing():
    d = regulatory_directive(EMA, "biosimilar_pk")
    assert "Article 10(4)" in d
    assert "Directive 2001/83/EC" in d
    assert "similar biological medicinal product" in d
    # EMA's comparator term is already correct for a biosimilar, so it is
    # inherited rather than overridden.
    assert "European Union reference medicinal product" in d


def test_ema_small_molecule_uses_the_eu_generic_pathway():
    d = regulatory_directive(EMA, "BE")
    assert "Article 10(1)" in d
    assert "351(k)" not in d
    assert "ANDA" not in _facts(d)


@pytest.mark.parametrize("gid", (M13A, M11))
@pytest.mark.parametrize("study_type", ("BE", "biosimilar_pk", "food_effect"))
def test_ich_asserts_no_submission_pathway(gid, study_type):
    """ICH harmonises requirements; it does not license products. Rather than
    guess a jurisdiction, the pathway line is omitted entirely."""
    d = regulatory_directive(gid, study_type)
    assert "- Submission pathway:" not in d
    assert "351(k)" not in d
    assert "ANDA" not in _facts(d)
    assert "Article 10(" not in d
    assert submission_pathway(gid, study_type) == ""


def test_m13a_biosimilar_does_not_cite_a_solid_oral_be_guideline_as_governing():
    """M13A is scoped to immediate-release SOLID ORAL dosage forms, so citing it
    as the governing guideline for a biologic would be wrong."""
    c = governing_citation(M13A, "biosimilar_pk")
    assert "biosimilar guidance of the regulatory region of submission" in c
    assert c != get_profile(M13A).citation
    # ICH M11 is a modality-agnostic protocol TEMPLATE: correct as-is.
    assert governing_citation(M11, "biosimilar_pk") == get_profile(M11).citation


# ---------------------------------------------------------------------------
# Accessors / registry hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("biosimilar_pk", True),
        ("BIOSIMILAR_PK", True),
        ("  biosimilar_pk  ", True),
        ("BE", False),
        ("food_effect", False),
        ("", False),
    ],
)
def test_is_biosimilar_study_normalises(value, expected):
    assert is_biosimilar_study(value) is expected


def test_unknown_guideline_still_falls_back_to_fda_pathway():
    """get_profile()'s FDA fallback must extend to the pathway fields so an
    unknown/legacy guideline string never yields an empty pathway."""
    assert submission_pathway("MADE-UP-2099", "BE") == submission_pathway(FDA, "BE")
    assert submission_pathway("MADE-UP-2099", "biosimilar_pk") == submission_pathway(
        FDA, "biosimilar_pk"
    )


# ---------------------------------------------------------------------------
# guideline_display_name — the UI selector's own label
#
# Found via a live-site check: the selector kept showing "FDA - Bioequivalence
# (ANDA, 2014)" for a Biosimilar PK Similarity study even though the
# "Submission pathway" line right underneath it correctly said 351(k) - the
# label was reading a study-type-INDEPENDENT field (display_name) while every
# other piece of biosimilar framing on the same screen was already
# study-type-aware. Same defect class as the ANDA/RLD prompt-text bug this
# file otherwise pins, just visible in the selector instead of the document.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("study_type", SMALL_MOLECULE_TYPES)
def test_small_molecule_label_is_unaffected(study_type):
    assert guideline_display_name(FDA, study_type) == get_profile(FDA).display_name
    assert guideline_display_name(EMA, study_type) == get_profile(EMA).display_name


def test_fda_biosimilar_label_names_351k_not_anda():
    label = guideline_display_name(FDA, "biosimilar_pk")
    assert "351(k)" in label
    assert "ANDA" not in label
    assert label != get_profile(FDA).display_name


def test_fda_old_biosimilar_label_names_351k_not_anda():
    label = guideline_display_name(FDA_OLD, "biosimilar_pk")
    assert "351(k)" in label
    assert "ANDA" not in label


def test_ema_biosimilar_label_drops_generic_bioequivalence_wording():
    label = guideline_display_name(EMA, "biosimilar_pk")
    assert label != get_profile(EMA).display_name
    assert "Bioequivalence" not in label


@pytest.mark.parametrize("gid", (M13A, M11))
def test_ich_biosimilar_label_falls_back_to_the_generic_one(gid):
    """Neither ICH profile has a distinct biosimilar override: M13A because it
    is scoped to small-molecule solid-oral BE (its citation already redirects
    rather than asserting a pathway it does not have), M11 because its
    modality-agnostic template label is already accurate as-is."""
    assert guideline_display_name(gid, "biosimilar_pk") == get_profile(gid).display_name


def test_unknown_guideline_label_falls_back_to_fda_biosimilar_label():
    assert guideline_display_name("MADE-UP-2099", "biosimilar_pk") == guideline_display_name(
        FDA, "biosimilar_pk"
    )


def test_pathway_fields_stay_ascii():
    # The module mandates ASCII-only strings (no em/en-dashes).
    for prof in GUIDELINES.values():
        for field in (
            prof.submission_pathway,
            prof.biosimilar_pathway,
            prof.biosimilar_citation,
            prof.biosimilar_reference_product_term,
        ):
            assert field.isascii(), (prof.id, field)


def test_every_directive_stays_ascii():
    for gid in GUIDELINES:
        for st in ("BE", "biosimilar_pk", "food_effect"):
            assert regulatory_directive(gid, st).isascii(), (gid, st)


def test_only_fda_profiles_carry_us_statutory_pathways():
    for prof in GUIDELINES.values():
        blob = prof.submission_pathway + prof.biosimilar_pathway
        if prof.regulator != "FDA":
            assert "351(k)" not in blob, prof.id
            assert "ANDA" not in blob, prof.id


# ---------------------------------------------------------------------------
# The system prompt and the regulatory directive must not contradict each other
# ---------------------------------------------------------------------------


def test_system_prompt_matches_the_directive_pathway():
    from services.ai_service import _expertise_for

    fda_sys, label = _expertise_for("biosimilar_pk", FDA)
    assert label == "biosimilar PK similarity"
    assert "351(k)" in fda_sys
    # Unknown/blank guideline resolves to the FDA profile -> unchanged behaviour.
    assert _expertise_for("biosimilar_pk", None)[0] == fda_sys

    ema_sys, _ = _expertise_for("biosimilar_pk", EMA)
    assert "351(k)" not in ema_sys
    assert "BPCIA" not in ema_sys
    assert "EMA" in ema_sys
    assert "European Union reference medicinal product" in ema_sys


# ---------------------------------------------------------------------------
# Selector surface (GET /api/guidelines) exposes the same framing
# ---------------------------------------------------------------------------


async def test_guidelines_endpoint_exposes_pathway_fields():
    from routers.guidelines import list_guidelines

    body = await list_guidelines()
    by_id = {g["id"]: g for g in body["guidelines"]}

    fda = by_id[FDA]
    assert "ANDA" in fda["submission_pathway"]
    assert "351(k)" in fda["biosimilar_pathway"]
    assert "351(k)" in fda["biosimilar_citation"]
    assert "351(k)" in fda["biosimilar_display_name"]
    assert "ANDA" not in fda["biosimilar_display_name"]

    ema = by_id[EMA]
    assert "Article 10(4)" in ema["biosimilar_pathway"]
    assert "351(k)" not in ema["biosimilar_pathway"]
    assert ema["biosimilar_display_name"] != ema["display_name"]

    # ICH grants no marketing authorisation -> nothing for the UI to show.
    assert by_id[M13A]["submission_pathway"] == ""
    assert by_id[M13A]["biosimilar_pathway"] == ""
    # ...and neither ICH profile has a distinct biosimilar label - the
    # frontend falls back to display_name for both (see
    # test_ich_biosimilar_label_falls_back_to_the_generic_one).
    assert by_id[M13A]["biosimilar_display_name"] == ""
    assert by_id[M11]["biosimilar_display_name"] == ""
