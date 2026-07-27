"""Regulatory guideline registry.

The app generates bioequivalence / biosimilar-PK protocols against a *selected*
regulatory guideline. The structure of a BE protocol is largely harmonised
(ICH M13 exists precisely to harmonise FDA/EMA/etc.), and the 90% CI 80.00-125.00%
acceptance band for AUC and Cmax is shared across all three - so what actually
differs per guideline is the **framing**: the regulator and citation, the
reference-product terminology, the highly-variable-drug and NTI handling, and
the fasting/fed convention.

This module is the single source of truth for those differences. It drives:
  - the AI generation directive (services/ai_service._regulatory_directive),
    injected into both the per-section and the three-stage-pipeline prompts, so
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


def regulatory_directive(version: str, study_type: str = "BE") -> str:
    """A concise, authoritative regulatory-framework block injected into every
    section-generation prompt so the drafted text reflects the SELECTED
    guideline's regulator, terminology, citations, and acceptance framing -
    not a hardcoded FDA default."""
    p = get_profile(version)
    lines = [
        "\nREGULATORY FRAMEWORK - write the protocol to satisfy THIS guideline; use its",
        "terminology and cite it (do not default to FDA/ANDA language unless this IS FDA):",
        f"- Governing guideline: {p.citation}",
        f"- Regulator / region: {p.regulator} ({p.region})",
        f"- Reference/comparator product: refer to it as the {p.reference_product_term}",
        f"- Bioequivalence acceptance criteria: {p.acceptance_summary}",
        f"- Food conditions: {p.food_convention}",
    ]
    if study_type != "biosimilar_pk":
        lines.append(f"- Highly variable drugs: {p.hvd_approach}")
        lines.append(f"- Narrow therapeutic index: {p.nti_approach}")
    if p.structural_note:
        lines.append(f"- Protocol structure: {p.structural_note}")
    return "\n".join(lines) + "\n"
