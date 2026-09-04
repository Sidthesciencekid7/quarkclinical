"""The budgeting tool's rate card — pure data + pure functions, no DB, no ORM
writes, no I/O.

This module knows nothing about Budget / BudgetCategory / BudgetLineItem rows;
``services/budget_engine.py`` consumes it and does all the ORM work. Every
number here is an APPROXIMATE planning estimate calibrated against three real
CRO budget documents for comparable early-phase studies (two signed Axis
Clinicals budgets — an Abatacept biosimilar-PK study and an NPLATE/Romiplostim
study — plus one Certolizumab pegol Phase-1 RFP). See DISCLAIMER below; it is
surfaced verbatim in Budget.derivation and in the exported DOCX.

ESTIMATION RULE — this module exists BECAUSE the platform's earlier "never
invent a dollar amount" rule was explicitly overridden by the product owner for
this tool: the whole point of the budgeting tool is that a drug name alone
produces a complete, fully-priced budget with no manual pricing step. Every
number here is still traceable (services/budget_engine.py records exactly
which assumption produced which figure in Budget.derivation) — it is an
approximation, not an invention with no paper trail.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

from models import BudgetBucket, BudgetLineStatus

RATE_CARD_VERSION = "2026-09-rev1"
RATE_CARD_CURRENCY = "USD"
DISCLAIMER = (
    "Every dollar figure in this budget is an APPROXIMATE planning estimate generated "
    "from a rate card calibrated against real signed CRO budgets for comparable "
    "early-phase studies. It is not a quotation, not a bid, and not a negotiated price. "
    "Every number is editable."
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateCardLine:
    key: str                      # stable, globally unique -> BudgetLineItem.derivation_key
    label: str                    # -> BudgetLineItem.label
    unit_description: str         # -> BudgetLineItem.unit_description
    qty_basis: str                # name from the QTY_BASES vocabulary
    unit_cost: float | None       # simple-study rate (USD)
    unit_cost_complex: float | None = None   # complex-study rate; None falls back to unit_cost
    status: BudgetLineStatus = BudgetLineStatus.costed
    include_when: str | None = None   # predicate name from INCLUDE_PREDICATES; None = always
    basis: str = ""               # human-readable arithmetic sentence


@dataclass(frozen=True)
class RateCardCategory:
    name: str                     # MUST match budget_engine.DEFAULT_CATEGORIES names exactly
    bucket: BudgetBucket
    sort_order: int               # 1..17
    lines: tuple[RateCardLine, ...]
    special: str | None = None    # only "clinical_conduct" uses this
    out_of_scope_line: str | None = None  # emitted when no lines resolve


@dataclass
class StudyDrivers:
    """Every quantity driver the rate card can key on. Mutable so
    budget_engine.resolve_drivers-style code can fill it in one place."""
    drug_inn: str | None
    modality: str                 # "biologic" | "small_molecule" | "unknown"
    study_type: str                # "BE" | "biosimilar_pk" | "food_effect"
    complexity: str                # "simple" | "complex"
    n_enrolled: int
    n_alternates: int
    n_screen_failures: int
    n_screened: int
    screen_failure_rate_pct: float
    n_sites: int
    n_dosing_groups: int
    project_duration_months: float
    doses_per_subject: int
    n_pk_timepoints: int
    n_ada_timepoints: int
    n_visit_days: int
    periods: int | None
    design_key: str | None
    n_qa_audits: int
    n_monitoring_visits: int
    n_external_vendors: int
    is_randomized_multi_arm: bool
    bioanalysis_in_house: bool


# ---------------------------------------------------------------------------
# C. QTY_BASES — the quantity vocabulary. A dict-of-lambdas, NOT an if/elif —
# an unknown basis name is a programming error and must raise loudly.
# ---------------------------------------------------------------------------

QTY_BASES: dict[str, Callable[[StudyDrivers], float]] = {
    "per_study": lambda d: 1.0,
    "per_site": lambda d: d.n_sites,
    "per_enrolled": lambda d: d.n_enrolled,
    "per_screened": lambda d: d.n_screened,
    "per_screen_failure": lambda d: d.n_screen_failures,
    "per_alternate": lambda d: d.n_alternates,
    "per_dose": lambda d: d.n_enrolled * d.doses_per_subject,
    "per_month": lambda d: math.ceil(d.project_duration_months),
    "per_week": lambda d: round(d.project_duration_months * 4.33),
    "per_year": lambda d: max(1, math.ceil(d.project_duration_months / 12)),
    "per_meeting": lambda d: round(d.project_duration_months * 2),
    "per_vendor": lambda d: d.n_external_vendors,
    "per_audit": lambda d: d.n_qa_audits,
    "per_monitoring_visit": lambda d: d.n_monitoring_visits,
    "per_pk_sample": lambda d: d.n_pk_timepoints * d.n_enrolled + d.n_ada_timepoints * d.n_enrolled,
    "rate_card_only": lambda d: 0.0,
}


def quantity_for(line: RateCardLine, d: StudyDrivers) -> float:
    """Dispatch on line.qty_basis. Unknown basis raises KeyError — fail loudly,
    never silently produce a 0 for a typo'd basis name."""
    fn = QTY_BASES[line.qty_basis]
    return max(0.0, float(fn(d)))


# ---------------------------------------------------------------------------
# D. INCLUDE_PREDICATES
# ---------------------------------------------------------------------------

INCLUDE_PREDICATES: dict[str, Callable[[StudyDrivers], bool]] = {
    "randomized_multi_arm": lambda d: d.is_randomized_multi_arm,
    "large_study": lambda d: d.n_enrolled >= 100,
    "bioanalysis_in_house": lambda d: d.bioanalysis_in_house,
    "bioanalysis_outsourced": lambda d: not d.bioanalysis_in_house,
    "has_ada": lambda d: d.n_ada_timepoints > 0,
}


def should_include(line: RateCardLine, d: StudyDrivers) -> bool:
    if line.include_when is None:
        return True
    return INCLUDE_PREDICATES[line.include_when](d)


# ---------------------------------------------------------------------------
# E. Clinical-conduct constants — used by budget_engine.build_clinical_conduct_rows
# ---------------------------------------------------------------------------

CONDUCT_PK_DRAW_RATE = 45.0      # per PK draw (blended procedure rate, $35-90 band)
CONDUCT_ADA_DRAW_RATE = 65.0     # per ADA/immunogenicity draw
CONDUCT_VISIT_DAY_RATE = 185.0   # per subject-visit day, non-procedure site time ($150-260 band)
CONDUCT_TARGET_PER_ENROLLED = {"simple": 7900.0, "complex": 13700.0}
CONDUCT_SCREEN_FAILURE_RATIO = 0.18   # Abatacept: 1435/7871.50 = 0.182
CONDUCT_ALTERNATE_RATIO = 0.33        # Abatacept: 2577/7871.50 = 0.327
DEFAULT_PK_TIMEPOINTS = {"BE": 12, "food_effect": 12, "biosimilar_pk": 12}
DEFAULT_ADA_TIMEPOINTS = {"BE": 0, "food_effect": 0, "biosimilar_pk": 6}
DEFAULT_VISIT_DAYS = {"BE": 8, "food_effect": 8, "biosimilar_pk": 10}


# ---------------------------------------------------------------------------
# F. DEFAULT_STUDY_PROFILES — used ONLY when a value has no protocol source
# and no user value.
# ---------------------------------------------------------------------------

DEFAULT_STUDY_PROFILES: dict[str, dict] = {
    "BE": dict(
        n_enrolled=36, screen_failure_rate_pct=40.0, alternate_fraction=0.10,
        n_sites=1, n_dosing_groups=1, project_duration_months=6.0,
        doses_per_subject=2, complexity="simple", n_external_vendors=1,
    ),
    "food_effect": dict(
        n_enrolled=36, screen_failure_rate_pct=40.0, alternate_fraction=0.10,
        n_sites=1, n_dosing_groups=1, project_duration_months=6.0,
        doses_per_subject=2, complexity="simple", n_external_vendors=1,
    ),
    "biosimilar_pk": dict(
        n_enrolled=60, screen_failure_rate_pct=40.0, alternate_fraction=0.10,
        n_sites=1, n_dosing_groups=2, project_duration_months=11.0,
        doses_per_subject=1, complexity="complex", n_external_vendors=2,
    ),
}


def advertising_cost(d: StudyDrivers) -> float:
    """Scales with planned screened-subject count, clamped to the observed
    $30k-$60k band across the reference documents."""
    return float(min(60000, max(30000, round(d.n_screened * 350, -3))))


def unit_cost_for(line: RateCardLine, d: StudyDrivers) -> float | None:
    if line.key == "recruiting.advertising":
        return advertising_cost(d)
    if d.complexity == "complex" and line.unit_cost_complex is not None:
        return line.unit_cost_complex
    return line.unit_cost


# ---------------------------------------------------------------------------
# G. RATE_CARD — the 17 categories, in order. Names MUST be byte-identical to
# budget_engine.DEFAULT_CATEGORIES (which is now DERIVED from this).
# ---------------------------------------------------------------------------

RATE_CARD: tuple[RateCardCategory, ...] = (
    RateCardCategory(
        name="Recruiting", bucket=BudgetBucket.direct, sort_order=1,
        lines=(
            RateCardLine(
                key="recruiting.advertising", label="Advertising and promotional activities",
                unit_description="Per study", qty_basis="per_study",
                unit_cost=None,  # computed by advertising_cost() — see unit_cost_for
                basis="Approximate planning spend scaled with the planned screened-subject "
                      "count, clamped to the $30,000-$60,000 band observed across comparable "
                      "reference studies.",
            ),
            RateCardLine(
                key="recruiting.subject_recruitment", label="Subject Recruitment",
                unit_description="per screened subject", qty_basis="per_screened",
                unit_cost=75.0,
                basis="Approximate $75/screened subject, the midpoint of the $60-95 band "
                      "observed in the reference budgets.",
            ),
            RateCardLine(
                key="recruiting.vct_setup", label="VCT Set up",
                unit_description="Per study", qty_basis="per_study", unit_cost=1500.0,
                basis="Flat per-study setup fee, as in the reference budgets.",
            ),
            RateCardLine(
                key="recruiting.vct_duplicate_check",
                label="VCT Check for Duplicate study participation",
                unit_description="per screened subject", qty_basis="per_screened",
                unit_cost=25.0,
                basis="Approximate $25/screened subject, as in the reference budgets.",
            ),
        ),
    ),
    RateCardCategory(
        name="Clinical Conduct", bucket=BudgetBucket.direct, sort_order=2,
        lines=(), special="clinical_conduct",
    ),
    RateCardCategory(
        name="Study Set Up and Pharmacy", bucket=BudgetBucket.direct, sort_order=3,
        lines=(
            RateCardLine(
                key="setup.study_setup_initiation", label="Study Setup and Initiation",
                unit_description="Per study", qty_basis="per_study", unit_cost=11500.0,
                basis="Flat per-study setup fee, as in the reference budgets.",
            ),
            RateCardLine(
                key="setup.irb_submission",
                label="IRB Submission (initial; IRB fee itself is a pass-through)",
                unit_description="Per study", qty_basis="per_study", unit_cost=4500.0,
                basis="Flat per-study fee; the IRB's own fee is booked separately as a "
                      "pass-through cost.",
            ),
            RateCardLine(
                key="setup.protocol_training", label="Protocol Training",
                unit_description="Per study", qty_basis="per_study", unit_cost=4500.0,
                basis="Flat per-study fee, as in the reference budgets.",
            ),
            RateCardLine(
                key="setup.pharmacy_manual",
                label="Pharmacy Manual with blinding procedures",
                unit_description="Per study", qty_basis="per_study", unit_cost=2500.0,
                basis="Flat per-study fee, as in the reference budgets.",
            ),
            RateCardLine(
                key="setup.ip_receipt_reconciliation",
                label="IP Receipt, shipment on site and reconciliation",
                unit_description="Per study", qty_basis="per_study", unit_cost=2000.0,
                basis="Flat per-study fee, as in the reference budgets.",
            ),
            RateCardLine(
                key="setup.drug_dispensing", label="Drug Dispensing",
                unit_description="per dose", qty_basis="per_dose", unit_cost=85.0,
                basis="Approximate $85/dose x enrolled subjects x doses/subject.",
            ),
            RateCardLine(
                key="setup.drug_storage", label="Drug Storage",
                unit_description="per year", qty_basis="per_year", unit_cost=1200.0,
                basis="Approximate $1,200/year, ceil(project duration / 12 months), minimum 1.",
            ),
        ),
    ),
    RateCardCategory(
        name="Bioanalytical / PD Analysis", bucket=BudgetBucket.direct, sort_order=4,
        lines=(
            RateCardLine(
                key="bioanalytical.sample_analysis_outsourced",
                label="PK/PD sample bioanalysis",
                unit_description="per sample", qty_basis="per_pk_sample", unit_cost=35.0,
                status=BudgetLineStatus.outsourced, include_when="bioanalysis_outsourced",
                basis="Outsourced to a specialty bioanalytical vendor, as in both reference "
                      "budgets. The per-sample rate is shown for reference only; no total is "
                      "carried into the rollup until the vendor quote is in.",
            ),
            RateCardLine(
                key="bioanalytical.sample_analysis_in_house",
                label="PK/PD sample bioanalysis (in-house)",
                unit_description="per sample", qty_basis="per_pk_sample", unit_cost=35.0,
                status=BudgetLineStatus.costed, include_when="bioanalysis_in_house",
                basis="Approximate $35/sample across the protocol's total PK/PD sample count.",
            ),
        ),
    ),
    RateCardCategory(
        name="Data Management", bucket=BudgetBucket.direct, sort_order=5,
        lines=(
            RateCardLine(
                key="dm.edc_license", label="EDC License Fee",
                unit_description="per subject", qty_basis="per_enrolled", unit_cost=175.0,
                basis="Approximate $175/enrolled subject.",
            ),
            RateCardLine(
                key="dm.data_management_plan", label="Data Management Plan",
                unit_description="Per study", qty_basis="per_study", unit_cost=2500.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="dm.edc_setup_ecrf", label="EDC set up and eCRF Development",
                unit_description="Per study", qty_basis="per_study", unit_cost=7500.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="dm.qc_uat", label="QC and UAT of database and eCRF",
                unit_description="Per study", qty_basis="per_study", unit_cost=4500.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="dm.query_resolution", label="Data management and Query resolution",
                unit_description="per subject", qty_basis="per_enrolled", unit_cost=250.0,
                basis="Approximate $250/enrolled subject.",
            ),
            RateCardLine(
                key="dm.meddra_who_coding", label="MedDRA and WHO Coding",
                unit_description="per subject", qty_basis="per_enrolled", unit_cost=75.0,
                basis="Approximate $75/enrolled subject.",
            ),
            RateCardLine(
                key="dm.lab_data_reconciliation",
                label="Bioanalytical Lab data Reconciliation",
                unit_description="Per study", qty_basis="per_study", unit_cost=500.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="dm.edc_listings",
                label="EDC listings of AE/SAE/ISR/Protocol Deviations",
                unit_description="per month", qty_basis="per_month", unit_cost=1500.0,
                basis="Approximate $1,500/month x ceil(project duration in months).",
            ),
            RateCardLine(
                key="dm.database_lock_archival", label="Database lock and Archival",
                unit_description="Per study", qty_basis="per_study", unit_cost=1250.0,
                basis="Flat per-study fee.",
            ),
        ),
    ),
    RateCardCategory(
        name="Biostatistics", bucket=BudgetBucket.direct, sort_order=6,
        lines=(
            RateCardLine(
                key="biostat.randomization_codes", label="Develop Randomization codes",
                unit_description="Per study", qty_basis="per_study", unit_cost=900.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="biostat.sap_mock_shells",
                label="Statistical Analysis Plan and Mock Shells",
                unit_description="Per study", qty_basis="per_study",
                unit_cost=12000.0, unit_cost_complex=14000.0,
                basis="Flat per-study fee ($12,000 simple / $14,000 complex study).",
            ),
            RateCardLine(
                key="biostat.tlf_programming", label="Programming TLFs",
                unit_description="Per study", qty_basis="per_study",
                unit_cost=12500.0, unit_cost_complex=14500.0,
                basis="Flat per-study fee ($12,500 simple / $14,500 complex study).",
            ),
            RateCardLine(
                key="biostat.cdisc_sdtm_mapping",
                label="CDISC/SDTM mapping and programming",
                unit_description="Per study", qty_basis="per_study",
                unit_cost=12000.0, unit_cost_complex=14000.0,
                basis="Flat per-study fee ($12,000 simple / $14,000 complex study).",
            ),
            RateCardLine(
                key="biostat.data_import_dta",
                label="Data Import/DTA (vendor data, e.g. immunogenicity or PK)",
                unit_description="per vendor", qty_basis="per_vendor", unit_cost=3000.0,
                basis="Approximate $3,000/external data vendor.",
            ),
            RateCardLine(
                key="biostat.final_statistical_analysis", label="Final Statistical Analysis",
                unit_description="Per study", qty_basis="per_study",
                unit_cost=18000.0, unit_cost_complex=20000.0,
                basis="Flat per-study fee ($18,000 simple / $20,000 complex study).",
            ),
            RateCardLine(
                key="biostat.tlf_production_review",
                label="Production and Review of TLFs prior to DBL",
                unit_description="Per study", qty_basis="per_study",
                unit_cost=12000.0, unit_cost_complex=15000.0,
                basis="Flat per-study fee ($12,000 simple / $15,000 complex study).",
            ),
            RateCardLine(
                key="biostat.final_cdisc_deliverables",
                label="Final CDISC deliverables (SDTM/ADaM/SDRG/ADRG/define.xml/aCRF)",
                unit_description="Per study", qty_basis="per_study",
                unit_cost=20000.0, unit_cost_complex=22000.0,
                basis="Flat per-study fee ($20,000 simple / $22,000 complex study).",
            ),
        ),
    ),
    RateCardCategory(
        name="Scientific Affairs", bucket=BudgetBucket.direct, sort_order=7,
        lines=(
            RateCardLine(
                key="sci.protocol_review",
                label="Protocol Review (including 1 amendment)",
                unit_description="Per study", qty_basis="per_study",
                unit_cost=3000.0, unit_cost_complex=5500.0,
                basis="Flat per-study fee ($3,000 simple / $5,500 complex study).",
            ),
            RateCardLine(
                key="sci.icf_development", label="ICF Development",
                unit_description="Per study", qty_basis="per_study", unit_cost=3000.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="sci.clinical_study_report", label="Clinical Study Report",
                unit_description="Per study", qty_basis="per_study", unit_cost=16000.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="sci.ectd_submission", label="eCTD Submission Deliverable",
                unit_description="Per study", qty_basis="per_study", unit_cost=4500.0,
                basis="Flat per-study fee.",
            ),
        ),
    ),
    RateCardCategory(
        name="Quality Assurance", bucket=BudgetBucket.direct, sort_order=8,
        lines=(
            RateCardLine(
                key="qa.site_audits", label="Site QA Audits",
                unit_description="per audit", qty_basis="per_audit", unit_cost=1600.0,
                basis="Approximate $1,600/audit; audit count scales with study duration.",
            ),
            RateCardLine(
                key="qa.tmf_audit", label="TMF Audit",
                unit_description="Per study", qty_basis="per_study", unit_cost=3500.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="qa.data_listing_review", label="Data Listing Review",
                unit_description="Per study", qty_basis="per_study", unit_cost=4500.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="qa.tlf_review", label="TLFs Review",
                unit_description="Per study", qty_basis="per_study", unit_cost=3500.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="qa.csr_review", label="Clinical Study Report Review",
                unit_description="Per study", qty_basis="per_study", unit_cost=2500.0,
                basis="Flat per-study fee.",
            ),
        ),
    ),
    RateCardCategory(
        name="Study Management and Administrative / Project Management",
        bucket=BudgetBucket.direct, sort_order=9,
        lines=(
            RateCardLine(
                key="pm.project_management_plan", label="Project Management Plan",
                unit_description="Per study", qty_basis="per_study", unit_cost=4000.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="pm.kick_off_meeting", label="Kick Off Meeting",
                unit_description="Per study", qty_basis="per_study", unit_cost=2000.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="pm.sponsor_telecons", label="Sponsor Team Telecons",
                unit_description="per meeting", qty_basis="per_meeting", unit_cost=1200.0,
                basis="Approximate $1,200/meeting, biweekly (2x project duration in months).",
            ),
            RateCardLine(
                key="pm.project_management", label="Project Management",
                unit_description="per week", qty_basis="per_week", unit_cost=1600.0,
                basis="Approximate $1,600/week x project duration in weeks "
                      "(months x 4.33).",
            ),
            RateCardLine(
                key="pm.etmf", label="eTMF Setup and maintenance",
                unit_description="Per study", qty_basis="per_study", unit_cost=4500.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="pm.iwrs", label="IWRS Setup and maintenance",
                unit_description="Per study", qty_basis="per_study", unit_cost=3750.0,
                include_when="randomized_multi_arm",
                basis="Flat per-study fee, only for randomized/multi-arm designs.",
            ),
        ),
    ),
    RateCardCategory(
        name="Clinical Monitoring", bucket=BudgetBucket.direct, sort_order=10,
        out_of_scope_line="Clinical monitoring (Out of Scope for this study size)",
        lines=(
            RateCardLine(
                key="monitoring.monitoring_plan", label="Clinical Monitoring Plan",
                unit_description="Per study", qty_basis="per_study", unit_cost=3000.0,
                include_when="large_study",
                basis="Flat per-study fee; included only for larger studies "
                      "(n_enrolled >= 100), matching the reference documents.",
            ),
            RateCardLine(
                key="monitoring.siv", label="Site Initiation Visit",
                unit_description="per site", qty_basis="per_site", unit_cost=2500.0,
                include_when="large_study",
                basis="Approximate $2,500/site; included only for larger studies.",
            ),
            RateCardLine(
                key="monitoring.smv", label="Site Monitoring Visits",
                unit_description="per visit", qty_basis="per_monitoring_visit", unit_cost=2400.0,
                include_when="large_study",
                basis="Approximate $2,400/monitoring visit; included only for larger studies.",
            ),
            RateCardLine(
                key="monitoring.scv", label="Site Close-out Visit",
                unit_description="per site", qty_basis="per_site", unit_cost=3200.0,
                include_when="large_study",
                basis="Approximate $3,200/site; included only for larger studies.",
            ),
            RateCardLine(
                key="monitoring.coordination",
                label="Monitoring coordination and report review",
                unit_description="Per study", qty_basis="per_study", unit_cost=4000.0,
                include_when="large_study",
                basis="Flat per-study fee; included only for larger studies.",
            ),
        ),
    ),
    RateCardCategory(
        name="Medical Monitoring", bucket=BudgetBucket.direct, sort_order=11,
        lines=(),
        out_of_scope_line="Medical Monitoring (Out of Scope — retained by the sponsor)",
    ),
    RateCardCategory(
        name="Pharmacovigilance and Safety Monitoring", bucket=BudgetBucket.direct, sort_order=12,
        lines=(
            RateCardLine(
                key="pv.safety_management_plan", label="Safety Management Plan",
                unit_description="Per study", qty_basis="per_study", unit_cost=2500.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="pv.sae_24h_coverage", label="SAE 24-hour coverage",
                unit_description="Per study", qty_basis="per_study",
                unit_cost=10000.0, unit_cost_complex=20000.0,
                basis="Flat per-study fee ($10,000 simple / $20,000 complex study).",
            ),
            RateCardLine(
                key="pv.ind_safety_report_support",
                label="IND Safety report preparation support",
                unit_description="Per study", qty_basis="per_study", unit_cost=2500.0,
                basis="Flat per-study fee.",
            ),
        ),
    ),
    RateCardCategory(
        name="Regulatory", bucket=BudgetBucket.direct, sort_order=13,
        lines=(),
        out_of_scope_line="Regulatory submissions (Out of Scope — sponsor responsibility)",
    ),
    RateCardCategory(
        name="Administrative Overheads", bucket=BudgetBucket.direct, sort_order=14,
        lines=(
            RateCardLine(
                key="admin.liability_insurance", label="Gen and Pro Liability insurance",
                unit_description="Per study", qty_basis="per_study", unit_cost=4500.0,
                basis="Flat per-study fee.",
            ),
            RateCardLine(
                key="admin.administrative_overheads", label="Administrative Overheads",
                unit_description="Per study", qty_basis="per_study", unit_cost=9000.0,
                basis="Flat per-study fee.",
            ),
        ),
    ),
    RateCardCategory(
        name="Subject Stipend", bucket=BudgetBucket.stipend, sort_order=15,
        lines=(
            RateCardLine(
                key="stipend.screen_failure", label="Screen Failure stipend",
                unit_description="per screen failure", qty_basis="per_screen_failure",
                unit_cost=100.0,
                basis="Approximate $100/screen failure.",
            ),
            RateCardLine(
                key="stipend.alternate", label="Alternate Subject stipend",
                unit_description="per alternate subject", qty_basis="per_alternate",
                unit_cost=250.0,
                basis="Approximate $250/alternate subject.",
            ),
            RateCardLine(
                key="stipend.completed", label="Dosed/Completed Subject stipend",
                unit_description="per subject", qty_basis="per_enrolled",
                unit_cost=2700.0, unit_cost_complex=4000.0,
                basis="Approximate $2,700/enrolled subject for a short single-dose BE study, "
                      "up to $4,000 for a longer biosimilar-PK study with more visits.",
            ),
        ),
    ),
    RateCardCategory(
        name="CRO/Vendor Variable Costs", bucket=BudgetBucket.variable, sort_order=16,
        lines=(
            RateCardLine(
                key="variable.icf_amendments", label="Develop ICF amendments",
                unit_description="per amendment", qty_basis="rate_card_only",
                unit_cost=1500.0, status=BudgetLineStatus.actuals_only,
                basis="Rate-card only ($1,200-1,800/amendment); priced but not yet incurred, "
                      "as in the reference documents' own 'Actuals incurred' convention.",
            ),
            RateCardLine(
                key="variable.protocol_amendments", label="Develop Protocol amendments",
                unit_description="per amendment", qty_basis="rate_card_only",
                unit_cost=1500.0, status=BudgetLineStatus.actuals_only,
                basis="Rate-card only ($1,200-1,800/amendment); priced but not yet incurred.",
            ),
            RateCardLine(
                key="variable.irb_resubmissions", label="IRB re-submissions",
                unit_description="per submission", qty_basis="rate_card_only",
                unit_cost=1500.0, status=BudgetLineStatus.actuals_only,
                basis="Rate-card only ($1,500/submission); priced but not yet incurred.",
            ),
            RateCardLine(
                key="variable.sae_data_reconciliation", label="SAE data reconciliation",
                unit_description="per SAE", qty_basis="rate_card_only",
                unit_cost=500.0, status=BudgetLineStatus.actuals_only,
                basis="Rate-card only ($500/SAE); priced but not yet incurred.",
            ),
            RateCardLine(
                key="variable.sae_receipt_processing", label="SAE receipt and processing",
                unit_description="per SAE", qty_basis="rate_card_only",
                unit_cost=900.0, status=BudgetLineStatus.actuals_only,
                basis="Rate-card only ($800-1,000/SAE); priced but not yet incurred.",
            ),
            RateCardLine(
                key="variable.sae_narrative", label="SAE narrative writing",
                unit_description="per SAE", qty_basis="rate_card_only",
                unit_cost=550.0, status=BudgetLineStatus.actuals_only,
                basis="Rate-card only ($500-600/SAE); priced but not yet incurred.",
            ),
            RateCardLine(
                key="variable.sample_shipment",
                label="Sample shipment (packaging, dry ice, logistics)",
                unit_description="per shipment", qty_basis="rate_card_only",
                unit_cost=250.0, status=BudgetLineStatus.actuals_only,
                basis="Rate-card only ($250/shipment); priced but not yet incurred.",
            ),
        ),
    ),
    RateCardCategory(
        name="Pass-Through Costs", bucket=BudgetBucket.pass_through, sort_order=17,
        lines=(
            RateCardLine(
                key="passthrough.irb_fee", label="IRB Fee",
                unit_description="Per study (actuals)", qty_basis="rate_card_only",
                unit_cost=4000.0, status=BudgetLineStatus.actuals_only,
                basis="Rate-card only ($4,000); priced but not yet incurred — a real "
                      "pass-through actual once the IRB invoices.",
            ),
            RateCardLine(
                key="passthrough.shipping_courier", label="Shipping Courier Costs",
                unit_description="Per study (actuals)", qty_basis="rate_card_only",
                unit_cost=1250.0, status=BudgetLineStatus.actuals_only,
                basis="Rate-card only ($1,250); priced but not yet incurred.",
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# H. Public lookup helpers
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def category_by_name() -> dict[str, RateCardCategory]:
    return {c.name: c for c in RATE_CARD}


def default_categories() -> list[tuple[str, BudgetBucket]]:
    """Single source of truth for the 17-row seed template — replaces the old
    hardcoded budget_engine.DEFAULT_CATEGORIES list literal."""
    return [(c.name, c.bucket) for c in RATE_CARD]


# ---------------------------------------------------------------------------
# I. Self-consistency guard — a duplicated key would silently collapse two
# rows during reconciliation, so fail at import time instead.
# ---------------------------------------------------------------------------

assert len(RATE_CARD) == 17, f"expected 17 rate-card categories, found {len(RATE_CARD)}"
_all_keys = [l.key for c in RATE_CARD for l in c.lines]
assert len(set(_all_keys)) == len(_all_keys), "duplicate RateCardLine.key detected"
del _all_keys
