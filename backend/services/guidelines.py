"""Regulatory guideline registry.

The app generates bioequivalence / biosimilar-PK protocols against a *selected*
regulatory guideline. The structure of a BE protocol is largely harmonised
(ICH M13 exists precisely to harmonise FDA/EMA/etc.), and the 90% CI 80.00-125.00%
acceptance band for AUC and Cmax is shared across all three - so what actually
differs per guideline is the **framing**: the regulator and citation, the
reference-product terminology, the highly-variable-drug and NTI handling, and
the fasting/fed convention.

The framing also differs by STUDY TYPE, not only by guideline: a generic small
molecule and a biosimilar are submitted under different statutes even to the
same regulator (US: an ANDA under FD&C Act 505(j) vs a 351(k) application under
the Public Health Service Act/BPCIA; EU: Directive 2001/83/EC Article 10(1) vs
Article 10(4)), and the comparator is named differently in each (Reference
Listed Drug vs US-licensed reference product). The ``biosimilar_*`` profile
fields below carry that split.

This module is the single source of truth for those differences. It drives:
  - the AI generation directive (regulatory_directive, below), injected into
    both the per-section and the three-stage-pipeline prompts, so
    an EMA protocol says "EU reference medicinal product" and cites the EMA
    guideline instead of the FDA ANDA guidance;
  - the frontend guideline selector (GET /api/guidelines);
  - guideline_version validation and the guideline_outdated flag.

Numeric acceptance limits still come from the biostatistics engine (they are
harmonised); guideline-specific limit divergence (e.g. EMA Cmax widening for
highly-variable drugs) is described in the directive text and is a future
engine refinement, not silently applied to the computed N.

NOTE: this file is intentionally ASCII-only (hyphens, not em/en-dashes) so the
strings survive being served as JSON and injected into prompts without any
encoding fragility.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GuidelineProfile:
    id: str                       # stored in Protocol.guideline_version
    display_name: str             # shown in the UI selector
    regulator: str                # e.g. "FDA", "EMA", "ICH"
    region: str                   # e.g. "United States", "European Union", "International (ICH)"
    citation: str                 # the full guidance citation
    reference_product_term: str   # what the comparator is called
    acceptance_summary: str       # the BE acceptance-criteria framing
    hvd_approach: str             # highly-variable-drug handling
    nti_approach: str             # narrow-therapeutic-index handling
    food_convention: str          # fasting/fed convention
    is_current: bool = True       # False => superseded (drives guideline_outdated)
    study_types: tuple[str, ...] = ("BE", "biosimilar_pk", "food_effect")
    structural_note: str = ""     # protocol-structure guidance (ICH M11); appended to the directive only when set

    # --- Submission pathway (study-type dependent) -------------------------
    # ``submission_pathway`` is the marketing application a small-molecule
    # BE / food-effect study supports; ``biosimilar_pathway`` is the (different)
    # one a biosimilar PK-similarity study supports. The ``biosimilar_*``
    # override fields apply ONLY when study_type == "biosimilar_pk", and only
    # where the biosimilar wording genuinely differs - an empty override falls
    # back to the generic field, so EMA (whose comparator is the EU reference
    # medicinal product either way) does not have to repeat itself.
    #
    # IMPORTANT: 351(k)/ANDA are US statutory constructs and Article 10(4) is an
    # EU one. Each profile states only its OWN regulator's pathway, and an empty
    # string means "this body does not grant marketing authorisations" (ICH) -
    # regulatory_directive() then OMITS the pathway line rather than asserting a
    # pathway that does not apply to the selected regulator.
    submission_pathway: str = ""
    biosimilar_pathway: str = ""
    biosimilar_citation: str = ""
    biosimilar_reference_product_term: str = ""
    # The UI selector's own label for a biosimilar study. ``display_name`` is
    # written in generic-drug terms ("Bioequivalence (ANDA, 2014)") for every
    # profile, so showing it unconditionally for a biosimilar_pk study puts a
    # small-molecule pathway name on screen right next to a correctly-computed
    # 351(k)/Article-10(4) "Submission pathway" line below it - the same class
    # of internal contradiction the regulatory_directive() prompt text was
    # fixed to avoid, just visible in the UI instead of the generated document.
    # Empty when the generic display_name is already accurate for a biosimilar
    # (ICH M11's modality-agnostic template label, or ICH M13A, whose citation
    # already redirects rather than asserting a pathway it does not have) -
    # the override then falls back to display_name, same pattern as the other
    # biosimilar_* fields.
    biosimilar_display_name: str = ""
    # Comparator-sourcing statement for a biosimilar study. Carries the
    # scientific-bridge requirement that using an EU-approved comparator on a
    # 351(k) filing creates - see _FDA_BIOSIMILAR_BRIDGE. Empty means the
    # profile asserts no comparator-sourcing condition.
    biosimilar_comparator_bridge: str = ""


# The FDA biosimilar framing is shared by every FDA profile: which small-molecule
# BE guidance a protocol is locked to (the 2014 ANDA guidance or the superseded
# 2003 one) has no bearing on the statutory pathway a BIOLOGIC follows, which is
# 351(k) either way.
_FDA_ANDA_PATHWAY = (
    "FDA Abbreviated New Drug Application (ANDA) under section 505(j) of the Federal "
    "Food, Drug, and Cosmetic Act (generic drug pathway)"
)
_FDA_351K_PATHWAY = (
    "FDA 351(k) biosimilar biologics license application under section 351(k) of the "
    "Public Health Service Act, as established by the Biologics Price Competition and "
    "Innovation Act (BPCIA) - a biologics licensure pathway, not a generic-drug pathway"
)
_FDA_BIOSIMILAR_CITATION = (
    "FDA Guidance for Industry: Scientific Considerations in Demonstrating Biosimilarity "
    "to a Reference Product (2015); FDA Guidance for Industry: Development of Therapeutic "
    "Protein Biosimilars - Comparative Analytical Assessment and Other Quality-Related "
    "Considerations (2019); FDA Guidance for Industry: Immunogenicity Testing of "
    "Therapeutic Protein Products (2019); Public Health Service Act section 351(k) (BPCIA)"
)
# COMPARATOR POLICY: the EU reference medicinal product is used as the
# comparator in this application's biosimilar PK-similarity studies, including
# those written to the FDA 351(k) pathway.
#
# This is a deliberate programme decision, not a default. Sourcing a single
# EU-licensed comparator lot for the whole clinical programme is the common
# development strategy: it avoids running the study twice against two
# separately-sourced comparators and lets one study support both an EU and a US
# filing.
#
# It carries a HARD REGULATORY CONDITION, and stating the comparator without
# stating the condition would be an error. Section 351(k)(2)(A)(i)(I) requires
# comparison against the US-LICENSED reference product. When a non-US-licensed
# comparator is used in a clinical study, FDA requires a SCIENTIFIC BRIDGE - a
# three-way comparison (proposed biosimilar / US-licensed reference product /
# EU-approved comparator) with analytical and, where needed, PK data - to
# justify the relevance of that study to the US-licensed product. The bridge is
# therefore emitted alongside the comparator term wherever the comparator is
# stated, so a protocol can never name an EU comparator on a 351(k) filing
# without also carrying the bridging requirement that makes it acceptable.
_FDA_BIOSIMILAR_REFERENCE_TERM = "EU reference medicinal product (EU-approved comparator)"
_FDA_BIOSIMILAR_BRIDGE = (
    "The comparator is the EU reference medicinal product. Section 351(k) requires the "
    "comparison to be against the US-licensed reference product, so a SCIENTIFIC BRIDGE is "
    "REQUIRED and must be described: a three-way comparative assessment of the proposed "
    "biosimilar, the US-licensed reference product, and the EU-approved comparator - "
    "comprising comparative analytical characterisation and, where the analytical bridge is "
    "insufficient, a three-way comparative PK study - to justify the relevance of data "
    "generated against the EU comparator to the US-licensed reference product. A single "
    "EU-approved comparator lot is used across the programme, and its identity, source, lot "
    "number and expiry must be documented. State the bridge explicitly; do NOT present the "
    "EU comparator as if it directly satisfied the 351(k) comparison on its own."
)
_EU_BIOSIMILAR_BRIDGE = (
    "The comparator is the EU reference medicinal product, sourced from the European Economic "
    "Area, which is the comparator the EU pathway itself requires - no bridging to another "
    "region's product is needed for an EU filing. Its identity, source, lot number and expiry "
    "must be documented, and a single comparator lot is used across the programme."
)


GUIDELINES: dict[str, GuidelineProfile] = {
    "FDA-BE-2014": GuidelineProfile(
        id="FDA-BE-2014",
        display_name="FDA - Bioequivalence (ANDA, 2014)",
        regulator="FDA",
        region="United States",
        citation=(
            "FDA Guidance for Industry: Bioequivalence Studies With Pharmacokinetic "
            "Endpoints for Drugs Submitted Under an ANDA (December 2014); 21 CFR Part 320"
        ),
        reference_product_term="Reference Listed Drug (RLD), US-sourced",
        acceptance_summary=(
            "The 90% confidence interval of the geometric mean Test/Reference ratio for "
            "AUC0-t, AUC0-inf and Cmax must fall within 80.00-125.00% (log-transformed data)."
        ),
        hvd_approach=(
            "For highly variable drugs (within-subject CV >= 30%), use the FDA reference-"
            "scaled average bioequivalence (RSABE) approach with a fully- or partially-"
            "replicated design and the regulatory constant sigma_w0 = 0.25."
        ),
        nti_approach=(
            "For narrow-therapeutic-index drugs, use a fully replicated design with FDA "
            "reference-scaling plus an upper-bound criterion on the within-subject "
            "standard-deviation ratio (tightened limits)."
        ),
        food_convention=(
            "Fasting and, where indicated by the RLD labeling, fed bioequivalence studies."
        ),
        submission_pathway=_FDA_ANDA_PATHWAY,
        biosimilar_pathway=_FDA_351K_PATHWAY,
        biosimilar_citation=_FDA_BIOSIMILAR_CITATION,
        biosimilar_reference_product_term=_FDA_BIOSIMILAR_REFERENCE_TERM,
        biosimilar_comparator_bridge=_FDA_BIOSIMILAR_BRIDGE,
        biosimilar_display_name="FDA - Biosimilar PK Similarity (351(k), 2014)",
    ),
    "EMA-BE-2010": GuidelineProfile(
        id="EMA-BE-2010",
        display_name="EMA - Investigation of Bioequivalence (2010)",
        regulator="EMA",
        region="European Union",
        citation=(
            "EMA Committee for Medicinal Products for Human Use (CHMP): Guideline on the "
            "Investigation of Bioequivalence, CPMP/EWP/QWP/1401/98 Rev. 1/Corr (2010)"
        ),
        reference_product_term="European Union reference medicinal product",
        acceptance_summary=(
            "The 90% confidence interval of the geometric mean Test/Reference ratio for "
            "AUC0-t and Cmax must fall within 80.00-125.00% (log-transformed data)."
        ),
        hvd_approach=(
            "For highly variable drugs/products (within-subject CV > 30% for Cmax), the "
            "acceptance range for Cmax may be widened up to a maximum of 69.84-143.19% "
            "using a scaled approach, provided the widening is pre-specified and clinically "
            "justified; AUC remains 80.00-125.00%."
        ),
        nti_approach=(
            "For narrow-therapeutic-index drugs, tighten the acceptance interval for AUC "
            "(and Cmax where relevant) to 90.00-111.11%."
        ),
        food_convention=(
            "Generally conducted under fasting conditions unless the SmPC recommends intake "
            "with food, in which case fed conditions apply."
        ),
        submission_pathway=(
            "European Union generic medicinal product application under Article 10(1) of "
            "Directive 2001/83/EC"
        ),
        biosimilar_pathway=(
            "European Union similar biological medicinal product (biosimilar) application "
            "under Article 10(4) of Directive 2001/83/EC"
        ),
        biosimilar_citation=(
            "EMA/CHMP Guideline on similar biological medicinal products, CHMP/437/04 Rev 1; "
            "EMA/CHMP Guideline on similar biological medicinal products containing "
            "biotechnology-derived proteins as active substance: non-clinical and clinical "
            "issues, EMEA/CHMP/BMWP/42832/2005 Rev. 1; Directive 2001/83/EC Article 10(4)"
        ),
        # No comparator override: the EU reference medicinal product is the
        # comparator for both a generic and a biosimilar application.
        biosimilar_display_name="EMA - Similar Biological Medicinal Products (2010)",
        # No biosimilar_reference_product_term override: EMA's generic
        # reference_product_term is already "European Union reference medicinal
        # product", which is exactly right for a biosimilar, so it is INHERITED.
        # Overriding it here with a reworded synonym would change the protocol's
        # comparator wording for no reason.
        biosimilar_comparator_bridge=_EU_BIOSIMILAR_BRIDGE,
    ),
    "ICH-M13A-2024": GuidelineProfile(
        id="ICH-M13A-2024",
        display_name="ICH M13A - BE for IR Solid Oral (2024)",
        regulator="ICH",
        region="International (ICH)",
        citation=(
            "ICH M13A: Bioequivalence for Immediate-Release Solid Oral Dosage Forms (2024), "
            "harmonised across ICH regulatory regions"
        ),
        reference_product_term="comparator product",
        acceptance_summary=(
            "The 90% confidence interval of the geometric mean Test/comparator ratio for the "
            "primary PK parameters (AUC and Cmax) must fall within 80.00-125.00% "
            "(log-transformed data), harmonised across ICH regions."
        ),
        hvd_approach=(
            "Highly variable drug handling follows the harmonised ICH M13 principles; "
            "reference-scaling specifics are addressed in the companion M13B/M13C documents."
        ),
        nti_approach=(
            "Narrow-therapeutic-index considerations follow the harmonised ICH M13 series; "
            "apply a tightened acceptance interval where the companion guidance requires it."
        ),
        food_convention=(
            "Fasting and/or fed studies per the harmonised M13A recommendations for the "
            "dosage form and the comparator's labeling."
        ),
        # ICH harmonises requirements; it does not grant marketing authorisations,
        # so no submission pathway is asserted (the line is omitted) - the pathway
        # belongs to whichever ICH region the sponsor actually files in.
        #
        # M13A is scoped to immediate-release SOLID ORAL dosage forms, i.e. small
        # molecules: citing it as the governing guideline for a biologic would be
        # wrong, so the biosimilar citation redirects instead of asserting it.
        # Phrased as a noun phrase so it reads correctly wherever a citation is
        # interpolated ("Governing guideline: ...", "working to ...").
        biosimilar_citation=(
            "the biosimilar guidance of the regulatory region of submission (ICH M13A "
            "itself covers bioequivalence for immediate-release solid oral dosage forms "
            "and does not govern biosimilar development)"
        ),
    ),
    "ICH-M11-2025": GuidelineProfile(
        id="ICH-M11-2025",
        display_name="ICH M11 - Harmonised Protocol Template (2025)",
        regulator="ICH",
        region="International (ICH)",
        citation=(
            "ICH M11: Clinical electronic Structured Harmonised Protocol (CeSHarP) / "
            "harmonised clinical trial protocol template (2025), harmonised across ICH "
            "regulatory regions"
        ),
        reference_product_term="comparator product",
        acceptance_summary=(
            "ICH M11 is a protocol TEMPLATE and structure standard, not a bioequivalence "
            "methodology standard: the bioequivalence acceptance criteria remain harmonised "
            "per ICH M13 (the 90% confidence interval of the geometric mean Test/comparator "
            "ratio for AUC and Cmax must fall within 80.00-125.00% on log-transformed data), "
            "while the protocol itself should follow the ICH M11 standardised section "
            "structure, section ordering, and terminology conventions."
        ),
        hvd_approach=(
            "Highly variable drug handling follows the harmonised ICH M13 principles; present "
            "that content within the ICH M11 harmonised section structure and terminology."
        ),
        nti_approach=(
            "Narrow-therapeutic-index considerations follow the harmonised ICH M13 series; "
            "document them using the ICH M11 template's section headings and conventions."
        ),
        food_convention=(
            "Fasting and/or fed studies per the harmonised ICH M13 recommendations for the "
            "dosage form and the comparator's labeling, described within the ICH M11 template "
            "structure."
        ),
        # ICH M11 is a modality-agnostic protocol TEMPLATE standard, so its
        # citation is correct for a biosimilar as well and needs no override.
        # As with M13A, no submission pathway is asserted: ICH does not license
        # products.
        structural_note=(
            "This protocol targets the ICH M11 harmonised clinical trial protocol template: "
            "align the section headings, section ordering, and terminology with the ICH M11 "
            "standard (CeSHarP) where the section taxonomy permits, without altering the "
            "scientific content required by the selected bioequivalence methodology."
        ),
    ),
    # Superseded — a KNOWN older guideline, so a protocol locked to it is valid
    # (passes creation validation) yet correctly reported as outdated. Excluded
    # from the selectable list (list_guidelines filters to is_current) so it
    # can't be chosen for NEW work, but recognised for legacy records.
    "FDA-BE-2003": GuidelineProfile(
        id="FDA-BE-2003",
        display_name="FDA - Bioavailability & Bioequivalence (2003, superseded)",
        regulator="FDA",
        region="United States",
        citation=(
            "FDA Guidance for Industry: Bioavailability and Bioequivalence Studies for "
            "Orally Administered Drug Products (2003) - superseded by the 2014 ANDA BE guidance"
        ),
        reference_product_term="Reference Listed Drug (RLD), US-sourced",
        acceptance_summary=(
            "The 90% confidence interval of the geometric mean Test/Reference ratio for AUC "
            "and Cmax must fall within 80.00-125.00% (log-transformed data)."
        ),
        hvd_approach="Superseded guidance; use the current FDA RSABE approach for new studies.",
        nti_approach="Superseded guidance; use the current FDA NTI approach for new studies.",
        food_convention="Fasting and fed studies per the (superseded) 2003 recommendations.",
        is_current=False,
        # Same FDA statutory framing as the 2014 profile: superseding a BE
        # guidance does not change which statute a generic or a biologic is
        # filed under.
        submission_pathway=_FDA_ANDA_PATHWAY,
        biosimilar_pathway=_FDA_351K_PATHWAY,
        biosimilar_citation=_FDA_BIOSIMILAR_CITATION,
        biosimilar_reference_product_term=_FDA_BIOSIMILAR_REFERENCE_TERM,
        biosimilar_comparator_bridge=_FDA_BIOSIMILAR_BRIDGE,
        biosimilar_display_name="FDA - Biosimilar PK Similarity (351(k), 2003, superseded)",
    ),
}

DEFAULT_GUIDELINE = "FDA-BE-2014"


def is_supported(version: str) -> bool:
    return version in GUIDELINES


def get_profile(version: str) -> GuidelineProfile:
    """Return the profile for a version, falling back to the FDA default so an
    unknown/legacy string never crashes generation."""
    return GUIDELINES.get(version, GUIDELINES[DEFAULT_GUIDELINE])


def guideline_display(version: str) -> str:
    return get_profile(version).display_name


def is_current(version: str) -> bool:
    """A guideline is 'current' if it's a known, non-superseded profile. Unknown
    strings are treated as outdated so legacy protocols surface for review."""
    prof = GUIDELINES.get(version)
    return bool(prof and prof.is_current)


def is_biosimilar_study(study_type: str) -> bool:
    """Single normalised check for the biosimilar branch, so every study-type
    split in this module keys off the same test."""
    return (study_type or "").strip().lower() == "biosimilar_pk"


def guideline_display_name(version: str, study_type: str = "BE") -> str:
    """The label the UI selector should show - the biosimilar override when the
    study is a biosimilar PK-similarity study and the profile defines one.

    Mirrors governing_citation()/reference_product_term() exactly. Without
    this, the selector's own label always reads the generic-drug wording
    ("Bioequivalence (ANDA, 2014)") even when the guideline text and the
    "Submission pathway" line shown next to it correctly say 351(k) - an
    internal contradiction visible on screen rather than in generated text.
    """
    p = get_profile(version)
    if is_biosimilar_study(study_type) and p.biosimilar_display_name:
        return p.biosimilar_display_name
    return p.display_name


def submission_pathway(version: str, study_type: str = "BE") -> str:
    """The marketing application this protocol is written to support.

    Empty string when the selected body does not grant marketing authorisations
    (ICH) - callers must then omit the pathway rather than substitute one.
    """
    p = get_profile(version)
    return p.biosimilar_pathway if is_biosimilar_study(study_type) else p.submission_pathway


def governing_citation(version: str, study_type: str = "BE") -> str:
    """The citation to write the protocol against - the biosimilar override when
    the study is a biosimilar PK-similarity study and the profile defines one."""
    p = get_profile(version)
    if is_biosimilar_study(study_type) and p.biosimilar_citation:
        return p.biosimilar_citation
    return p.citation


def reference_product_term(version: str, study_type: str = "BE") -> str:
    """What the comparator must be called. A biosimilar's comparator is the
    (US-licensed / EU) reference product - never a Reference Listed Drug, which
    is generic-drug (ANDA) terminology."""
    p = get_profile(version)
    if is_biosimilar_study(study_type) and p.biosimilar_reference_product_term:
        return p.biosimilar_reference_product_term
    return p.reference_product_term


def regulatory_directive(version: str, study_type: str = "BE") -> str:
    """A concise, authoritative regulatory-framework block injected into every
    section-generation prompt so the drafted text reflects the SELECTED
    guideline's regulator, terminology, citations, and acceptance framing -
    not a hardcoded FDA default.

    Study-type aware: a biosimilar PK-similarity study is framed to the
    biosimilar pathway (FDA 351(k)/BPCIA, EU Article 10(4)) with a reference
    PRODUCT comparator, and the generic-drug (ANDA/RLD) wording - including the
    RLD-labeling food convention and the small-molecule HVD/NTI approaches - is
    kept out of the prompt entirely.
    """
    p = get_profile(version)
    biosimilar = is_biosimilar_study(study_type)
    header = (
        "terminology and cite it. This is a BIOLOGIC (biosimilar) programme: use only the "
        "submission pathway and comparator terminology named below, never generic "
        "small-molecule submission wording, and cite no other jurisdiction's statute:"
        if biosimilar else
        "terminology and cite it (do not default to FDA/ANDA language unless this IS FDA):"
    )
    lines = [
        "\nREGULATORY FRAMEWORK - write the protocol to satisfy THIS guideline; use its",
        header,
        f"- Governing guideline: {governing_citation(version, study_type)}",
        f"- Regulator / region: {p.regulator} ({p.region})",
    ]
    pathway = submission_pathway(version, study_type)
    if pathway:
        lines.append(f"- Submission pathway: {pathway}")
    lines.append(
        "- Reference/comparator product: refer to it as the "
        f"{reference_product_term(version, study_type)}"
    )
    if biosimilar and p.biosimilar_comparator_bridge:
        # Emitted immediately after the comparator term, never separately: an
        # EU-approved comparator named on a 351(k) filing WITHOUT its scientific
        # bridge is a regulatory error, so the two must not be able to drift apart.
        lines.append(f"- Comparator sourcing and bridging: {p.biosimilar_comparator_bridge}")
    if biosimilar:
        lines.append(f"- PK similarity acceptance criteria: {p.acceptance_summary}")
    else:
        lines.append(f"- Bioequivalence acceptance criteria: {p.acceptance_summary}")
        # Food conditions / HVD / NTI are small-molecule oral-BE concepts (and the
        # FDA food convention is phrased in RLD terms), so they stay out of a
        # biosimilar prompt.
        lines.append(f"- Food conditions: {p.food_convention}")
        lines.append(f"- Highly variable drugs: {p.hvd_approach}")
        lines.append(f"- Narrow therapeutic index: {p.nti_approach}")
    if p.structural_note:
        lines.append(f"- Protocol structure: {p.structural_note}")
    return "\n".join(lines) + "\n"
