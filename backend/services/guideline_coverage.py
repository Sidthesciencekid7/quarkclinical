"""
Guideline coverage matrix — maps each regulatory requirement to the section(s)
that should satisfy it and reports covered / partial / missing.

Deterministic, no-AI (same spirit as ``protocol_qc``). Turns "the protocol looks
complete" into "every requirement the regulator checks is provably present."

References are the public FDA/ICH guidances the app targets:
  - FDA, *Bioequivalence Studies With PK Endpoints for Drugs Submitted Under an
    ANDA* (2014)
  - ICH M11 (Clinical electronic Structured Harmonised Protocol)
  - ICH E6(R2) Good Clinical Practice
  - ICH E9(R1) Statistical Principles / Estimands
  - FDA, *Scientific Considerations in Demonstrating Biosimilarity* (biosimilar)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class CovStatus(str, Enum):
    COVERED = "covered"
    PARTIAL = "partial"          # section present but expected content not detected
    MISSING = "missing"          # required section empty/absent
    NA = "not_applicable"


_SEVERITY = {CovStatus.MISSING: 0, CovStatus.PARTIAL: 1, CovStatus.COVERED: 2, CovStatus.NA: 3}


@dataclass(frozen=True)
class Requirement:
    id: str
    label: str
    guidance: str
    sections: tuple[str, ...]          # section(s) that may satisfy it
    terms: tuple[str, ...] = ()        # any present ⇒ covered (empty ⇒ presence is enough)
    study_types: tuple[str, ...] = ("BE", "biosimilar_pk", "food_effect")


# Ordered to read like a reviewer's checklist. Sections follow the reference
# clinical-trial-protocol template's 16-section taxonomy (see export.py /
# protocols.py SECTION_ORDER) — several requirements now span more than one
# section because the template distributes related content more granularly
# (e.g. Cmax/AUC appear in both "endpoints" and "statistical_analysis_plan").
_REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("identification", "Study identification & title", "ICH M11 §1", ("title",)),
    Requirement("rationale", "Study rationale & risk/benefit assessment", "ICH E6(R2) §3",
                ("study_rationale",), ("rationale", "risk", "benefit")),
    Requirement("objectives", "Objectives & endpoints", "FDA BE 2014; ICH E9(R1)",
                ("objectives", "endpoints"),
                ("bioequival", "equivalence", "pharmacokinetic", "primary", "cmax", "auc")),
    Requirement("design", "Study design (crossover/parallel)", "FDA BE 2014; ICH E6(R2) §6.4",
                ("study_design",), ("crossover", "parallel", "replicate", "randomi")),
    Requirement("randomization", "Randomization & blinding", "ICH E6(R2) §6.4",
                ("treatment", "study_design"), ("randomi", "blind", "sequence")),
    Requirement("reference_product", "Reference & test product identification", "FDA BE 2014 §III",
                ("treatment", "study_design"), ("reference", "test product", "rld", "listed drug", "batch", "lot")),
    Requirement("eligibility", "Eligibility (inclusion/exclusion)", "FDA BE 2014 §IV; ICH E6(R2) §6.5",
                ("population",), ("inclusion", "exclusion", "eligib")),
    Requirement("restrictions", "Subject restrictions (food/fluid/posture)", "FDA BE 2014 §IV",
                ("treatment", "population"),
                ("food", "fast", "fluid", "posture", "smoking", "caffeine", "alcohol", "grapefruit", "exercise")),
    Requirement("dosing", "Dosing & administration", "FDA BE 2014 §IV",
                ("treatment",), ("dose", "administ", "oral", "single", "fasted", "fed")),
    Requirement("washout", "Washout adequacy (crossover)", "FDA BE 2014 §IV",
                ("study_design", "treatment"), ("washout",)),
    Requirement("sampling", "PK sampling schedule", "FDA BE 2014 §V",
                ("study_assessments",), ("sampl", "blood", "time point", "timepoint", "hour")),
    Requirement("pk_parameters", "PK parameters (Cmax, AUC, Tmax)", "FDA BE 2014 §V",
                ("endpoints", "study_assessments", "statistical_analysis_plan"), ("cmax", "auc", "tmax")),
    Requirement("bioanalytical", "Validated bioanalytical method", "FDA Bioanalytical Method Validation 2018",
                ("study_assessments",), ("validat", "lloq", "lc-ms", "mass spectrom", "ligand", "elisa", "assay")),
    Requirement("statistics", "Statistical method (ANOVA, 90% CI)", "FDA BE 2014 §VI; ICH E9(R1)",
                ("statistical_analysis_plan",), ("anova", "confidence interval", "90%", "log-transform", "mixed model")),
    Requirement("acceptance", "BE acceptance criteria", "FDA BE 2014 §VI",
                ("statistical_analysis_plan",), ("80.00", "125.00", "80-125", "90.00", "111.11", "acceptance")),
    Requirement("sample_size", "Sample size justification", "ICH E9; FDA BE 2014",
                ("statistical_analysis_plan",), ("sample size", "power", "subjects", "evaluable")),
    Requirement("safety", "Safety assessments & AE monitoring", "ICH E6(R2) §6.8",
                ("safety_monitoring", "study_assessments"), ("adverse", "safety", "monitor", "vital")),
    Requirement("data_handling", "Data handling & record retention", "ICH E6(R2) §5.5",
                ("data_handling",), ("source document", "record", "retention", "database")),
    Requirement("study_monitoring", "Study monitoring, auditing & inspection rights",
                "ICH E6(R2) §5.18, §5.19", ("study_monitoring",), ("monitor", "audit", "inspect")),
    Requirement("ethics", "Ethics, IRB/IEC & informed consent", "ICH E6(R2) §3,4.8; Declaration of Helsinki",
                ("ethics",), ("ethic", "irb", "iec", "informed consent", "helsinki", "gcp", "institutional review")),
    Requirement("administrative", "Administrative procedures (amendments, insurance, publication)",
                "ICH E6(R2) §4.5, §5.8", ("administrative",), ("amendment", "insurance", "publicat")),
    Requirement("references", "Regulatory references", "—",
                ("references",), ("guidance", "ich", "fda", "cfr", "bioequivalence")),
    # Biosimilar-only
    Requirement("immunogenicity", "Immunogenicity (ADA/NAb) assessment",
                "FDA Biosimilarity 2015; FDA Immunogenicity 2019",
                ("study_assessments", "statistical_analysis_plan", "endpoints"),
                ("immunogenic", "anti-drug", "ada", "neutralizing", "nab"),
                study_types=("biosimilar_pk",)),
    Requirement("extended_followup", "Extended immunogenicity follow-up",
                "FDA Biosimilarity 2015",
                ("study_assessments", "safety_monitoring"),
                ("follow-up", "week 24", "day 169", "extended"),
                study_types=("biosimilar_pk",)),
)


@dataclass
class CoverageItem:
    id: str
    requirement: str
    guidance: str
    sections: list[str]
    status: CovStatus
    detail: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class CoverageReport:
    items: list[CoverageItem] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = {s.value: 0 for s in CovStatus}
        for it in self.items:
            c[it.status.value] += 1
        return c

    @property
    def coverage_pct(self) -> int:
        applicable = [it for it in self.items if it.status is not CovStatus.NA]
        if not applicable:
            return 0
        covered = sum(1 for it in applicable if it.status is CovStatus.COVERED)
        return round(100 * covered / len(applicable))

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage_pct": self.coverage_pct,
            "counts": self.counts,
            "total": len(self.items),
            "items": [it.to_dict() for it in self.items],
        }


def _present(sections: dict[str, str], keys: tuple[str, ...]) -> tuple[bool, str]:
    """Return (any_section_has_content, combined_lowercased_text)."""
    parts = [(sections.get(k) or "").strip() for k in keys]
    combined = "\n".join(p for p in parts if p)
    return (bool(combined), combined.lower())


def compute_coverage(
    *,
    study_type: str,
    population_config: dict[str, Any],
    sections: dict[str, str],
    section_labels: Optional[dict[str, str]] = None,
) -> CoverageReport:
    labels = section_labels or {}
    report = CoverageReport()
    ss = population_config.get("_sample_size") or {}
    design_key = ss.get("design_key")
    st_norm = (study_type or "").lower()

    for req in _REQUIREMENTS:
        if st_norm not in {t.lower() for t in req.study_types}:
            continue

        # Washout is not applicable to a parallel design.
        if req.id == "washout" and design_key == "parallel":
            report.items.append(CoverageItem(
                req.id, req.label, req.guidance, list(req.sections),
                CovStatus.NA, "Parallel design — washout not required."))
            continue

        has_content, text = _present(sections, req.sections)
        sec_names = ", ".join(labels.get(s, s) for s in req.sections)

        if not has_content:
            status, detail = CovStatus.MISSING, f"No content in: {sec_names}."
        elif not req.terms:
            status, detail = CovStatus.COVERED, f"Present in {sec_names}."
        elif any(t in text for t in req.terms):
            status, detail = CovStatus.COVERED, f"Addressed in {sec_names}."
        else:
            status, detail = CovStatus.PARTIAL, (
                f"{sec_names} present but expected content not detected "
                f"(looked for: {', '.join(req.terms[:4])}…)."
            )

        report.items.append(CoverageItem(
            req.id, req.label, req.guidance, list(req.sections), status, detail))

    # stable sort: worst-first, then declaration order preserved within status
    report.items.sort(key=lambda it: _SEVERITY[it.status])
    return report
