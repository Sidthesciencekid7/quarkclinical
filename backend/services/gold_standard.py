"""Gold-standard feature manifest — the canonical set of features every generated
protocol must contain, applied per study type.

This module is the single source of truth for "what a complete, high-quality
protocol looks like". It is consumed by ``protocol_qc.run_qc`` to verify each
generated protocol, and — because those QC findings feed the three-stage
pipeline's remediation pass (``services.three_stage_pipeline._qc_remediate``) —
any missing feature is automatically regenerated. The net effect is that every
protocol converges to the SAME applicable feature set rather than depending on
run-to-run AI variation.

Two kinds of features:
  * Content features (``ContentFeature``) live in an AI-generated section and are
    detected by keyword scan; when missing they become a QC WARNING tied to that
    section, which the remediation loop fixes.
  * Deterministic features (``DETERMINISTIC_FEATURES``) are always injected by the
    export layer (e.g. the study-design schematic, the sample-size table, the QC
    appendix). They are reported as informational PASS items so the gold-standard
    checklist is complete.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Study-type codes used across the app (schemas.StudyType values).
ALL_STUDY_TYPES = ("BE", "biosimilar_pk", "food_effect")
_BIOLOGIC = ("biosimilar_pk",)


def _section_text(sections: dict, key: str) -> str:
    """Lower-cased, tag-stripped text of one section for keyword detection."""
    raw = sections.get(key) or ""
    return re.sub(r"<[^>]+>", " ", raw).lower()


@dataclass(frozen=True)
class ContentFeature:
    """A feature that must appear in an AI-generated section."""

    id: str
    label: str
    section: str                       # section key the feature belongs to
    any_keywords: tuple[str, ...]      # present if ANY keyword appears (case-insensitive)
    applies_to: tuple[str, ...] = ALL_STUDY_TYPES
    rationale: str = ""                # actionable guidance — doubles as remediation instruction

    def applies(self, study_type: str) -> bool:
        return study_type in self.applies_to

    def present(self, sections: dict) -> bool:
        hay = _section_text(sections, self.section)
        return any(k.lower() in hay for k in self.any_keywords)


@dataclass(frozen=True)
class DeterministicFeature:
    """A feature the export layer always injects (no AI dependence)."""

    id: str
    label: str
    applies_to: tuple[str, ...] = ALL_STUDY_TYPES
    detail: str = ""

    def applies(self, study_type: str) -> bool:
        return study_type in self.applies_to


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------

CONTENT_FEATURES: list[ContentFeature] = [
    ContentFeature(
        "title_safety", "Title states Safety assessment", "title",
        ("safety", "tolerabilit"),
        rationale="The study title must state that Safety/tolerability is assessed, "
                  "in addition to the primary PK objective.",
    ),
    ContentFeature(
        "title_immunogenicity", "Title states Immunogenicity", "title",
        ("immunogenic",), applies_to=_BIOLOGIC,
        rationale="For a biosimilar, the title must explicitly include Immunogenicity "
                  "(e.g. 'Pharmacokinetic Similarity, Safety, and Immunogenicity').",
    ),
    ContentFeature(
        "primary_auc_inf", "Primary PK endpoint AUC0-∞", "endpoints",
        ("auc0-inf", "auc0–inf", "auc0-∞", "auc0–∞",
         "to infinity", "auc(0-inf", "aucinf", "auc 0-inf"),
        rationale="For a single-dose study, AUC0-inf (with Cmax) must be the co-PRIMARY "
                  "exposure endpoint — not AUC0-t.",
    ),
    ContentFeature(
        "secondary_auc_t", "AUC0-t reported as secondary", "endpoints",
        ("auc0-t", "auc0–t", "auc 0-t", "last measurable", "last quantifiable"),
        rationale="AUC0-t must appear as a supportive SECONDARY PK endpoint.",
    ),
    ContentFeature(
        "immunogenicity_endpoints", "Immunogenicity endpoints (ADA/NAb)", "endpoints",
        ("anti-drug antibod", "ada", "neutralizing antibod", "immunogenic", "nab"),
        applies_to=_BIOLOGIC,
        rationale="A biosimilar protocol must define immunogenicity endpoints "
                  "(incidence of anti-drug antibodies (ADA) and neutralizing antibodies (NAb)).",
    ),
    ContentFeature(
        "design_rationale", "Design choice (crossover vs parallel) justified", "study_design",
        ("washout", "parallel-group", "parallel group", "crossover",
         "intra-subject", "within-subject", "half-life"),
        rationale="The Study Design must justify crossover vs parallel from the drug's "
                  "terminal half-life and within-subject variability (crossover removes "
                  "intra-subject variability but needs a long washout for long-half-life drugs).",
    ),
    ContentFeature(
        "missing_data_handling", "Missing-data & discontinuation handling", "statistical_analysis_plan",
        ("missing", "consecutive", "discontinu", "withdraw", "per-protocol",
         "per protocol", "evaluab"),
        rationale="The SAP must specify handling of missing PK timepoints (e.g. >=3 consecutive "
                  "missing -> excluded) and of subjects who discontinue for adverse events vs "
                  "non-compliance.",
    ),
    ContentFeature(
        "cv_basis", "Sample-size CV basis stated", "statistical_analysis_plan",
        ("coefficient of variation", "within-subject cv", "intra-subject cv",
         "within-subject coefficient", "cv of", "cv%", "%cv", "cvw"),
        rationale="The SAP must state the within-subject CV used to power the study "
                  "(the highest observed across healthy-volunteer studies) and its source.",
    ),
    ContentFeature(
        "immunogenicity_analysis", "Immunogenicity analysis in SAP", "statistical_analysis_plan",
        ("immunogenic", "anti-drug", "ada"), applies_to=_BIOLOGIC,
        rationale="A biosimilar SAP must describe the immunogenicity (ADA/NAb) analysis.",
    ),
]

DETERMINISTIC_FEATURES: list[DeterministicFeature] = [
    DeterministicFeature(
        "schematic", "Study design schematic (Figure 1)",
        detail="A design schematic is auto-generated in the export from the locked design.",
    ),
    DeterministicFeature(
        "sample_size_table", "Sample-size determination table",
        detail="A deterministic sample-size/design table is auto-generated in the export.",
    ),
    DeterministicFeature(
        "qc_appendix", "Automated QC checklist appendix",
        detail="An automated QC checklist (including this gold-standard coverage) travels with the document.",
    ),
]


def applicable_content_features(study_type: str) -> list[ContentFeature]:
    return [f for f in CONTENT_FEATURES if f.applies(study_type)]


def applicable_deterministic_features(study_type: str) -> list[DeterministicFeature]:
    return [f for f in DETERMINISTIC_FEATURES if f.applies(study_type)]
