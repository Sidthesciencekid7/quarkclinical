"""The budgeting tool's rate card — pure data + pure functions, no DB, no ORM
writes, no I/O.

This module knows nothing about Budget / BudgetCategory / BudgetLineItem rows;
``services/budget_engine.py`` consumes it and does all the ORM work. It holds
TWO independent rate cards, selected by ``Budget.subject_profile``:

  HEALTHY_RATE_CARD (alias: RATE_CARD, kept for backward compatibility — this
      name is imported directly elsewhere) — the original 17-category
      healthy-volunteer card. UNTOUCHED by the patient-card work below: every
      rate, line, category and default in this section is byte-for-byte what
      it was before. It is an APPROXIMATE planning estimate calibrated against
      three real CRO budget documents for comparable early-phase studies (two
      signed Axis Clinicals budgets — an Abatacept biosimilar-PK study and an
      NPLATE/Romiplostim study — plus one Certolizumab pegol Phase-1 RFP). See
      DISCLAIMER below; it is surfaced verbatim in Budget.derivation and in
      the exported DOCX.

  PATIENT_RATE_CARD — a new 22-category card for multi-site, multi-country
      PATIENT (not healthy-volunteer) studies — the shape of a real oncology
      biosimilar trial: investigator grants, phase-based monthly pricing,
      monitoring-visit volume, SAE/SUSAR volume, medical monitoring in scope,
      software/hosting licensing, central lab, imaging/BICR, clinical supply
      and depot logistics, site ancillary equipment, and long-tail archival
      obligations. Calibrated against three real 2026 CRO bids for the same
      study (Novotech, PPD/Emmes, IQVIA — Kashiv Biosciences KSHB012-101, a
      nivolumab biosimilar in melanoma/oesophageal cancer) PLUS real,
      externally-published rates researched line-by-line. See
      PATIENT_DISCLAIMER, RateCardSource, PROVENANCE_TIERS and
      SOURCE_REGISTRY below — every researched figure carries its real
      citation; every unresearched approximation is plainly marked
      "estimate", never dressed up as "researched".

ESTIMATION RULE — this module exists BECAUSE the platform's earlier "never
invent a dollar amount" rule was explicitly overridden by the product owner for
this tool: the whole point of the budgeting tool is that a drug name alone
produces a complete, fully-priced budget with no manual pricing step. Every
number here is still traceable (services/budget_engine.py records exactly
which assumption produced which figure in Budget.derivation) — it is an
approximation, not an invention with no paper trail. The patient card raises
the bar further: wherever a real published rate exists, it is used and cited;
an approximation is used ONLY where the research explicitly found no public
figure, and is labelled as such rather than silently blended in.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

from models import BudgetBucket, BudgetLineStatus

RATE_CARD_VERSION = "2026-09-rev2"
RATE_CARD_CURRENCY = "USD"
DISCLAIMER = (
    "Every dollar figure in this budget is an APPROXIMATE planning estimate generated "
    "from a rate card calibrated against real signed CRO budgets for comparable "
    "early-phase studies. It is not a quotation, not a bid, and not a negotiated price. "
    "Every number is editable."
)

# Patient/oncology card version and disclaimer — kept separate from the
# healthy-volunteer card's RATE_CARD_VERSION/DISCLAIMER above so a change to
# one never silently touches the other.
PATIENT_RATE_CARD_VERSION = "2026-09-patient-rev1"
PATIENT_DISCLAIMER = (
    "Figures in this budget come from three sources, each labelled on every line. "
    "RESEARCHED figures are real published rates — institutional fee schedules, "
    "manufacturer list prices, government schedules and peer-reviewed benchmarks — "
    "cited in full in the Sources section. BID-CALIBRATED figures are unit rates taken "
    "from real CRO bids for a comparable patient oncology study; they are one vendor's "
    "price, not a market benchmark. ESTIMATED figures are approximations for items no "
    "public price exists for, and are flagged as such. Nothing here is a quotation, a "
    "bid, or a negotiated price. Every number is editable."
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
    # -- Patient-card-only fields (both default so the healthy card, which
    # never sets either, is unaffected) --------------------------------------
    source_ref: str | None = None    # key into SOURCE_REGISTRY; None = no citation attached
    geo_scaled: bool = False         # True only for lines carrying a US-published
                                      # institutional rate that should scale down for
                                      # non-US geographies (see GEOGRAPHY_FACTORS).
                                      # CRO-bid-calibrated rates are already
                                      # global-blended and must NOT be geo-scaled again.


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

    # -- Patient/oncology profile (all defaulted; the healthy-volunteer code
    # path never sets any of these and behaves exactly as before) -----------
    subject_profile: str = "healthy"        # "healthy" | "patient"
    therapeutic_area: str = "other"         # "oncology" | "other" | "unknown"
    indication: str | None = None
    n_countries: int = 1
    n_sites_selected: int = 1
    n_completed: int = 0
    n_dropouts: int = 0
    dropout_rate_pct: float = 0.0
    startup_months: float = 0.0
    recruitment_months: float = 0.0
    treatment_months: float = 0.0
    followup_months: float = 0.0
    closeout_months: float = 0.0
    n_expected_saes: int = 0
    n_expected_susars: int = 0
    n_imv_onsite: int = 0
    n_imv_remote: int = 0
    n_pharmacy_visits: int = 0
    n_siv: int = 0
    n_cov: int = 0
    n_qualification_visits: int = 0
    n_visits_per_patient: int = 0
    crf_pages_completer: int = 0
    crf_pages_dropout: int = 0
    n_protocol_amendments: int = 0
    n_depots: int = 0
    ip_shipments_per_site: int = 0
    imaging_timepoints_completer: int = 0
    imaging_timepoints_dropout: int = 0
    geography_factor: float = 1.0
    archival_retention_years: int = 0
    bicr_required: bool = False
    comparator_sourced_by_sponsor: bool = False
    comparator_arm_fraction: float = 0.0
    monitoring_remote_fraction: float = 0.20   # default 80/20 on-site/remote split


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
# C2. Patient-card quantity bases — extends QTY_BASES with the vocabulary
# PATIENT_RATE_CARD's lines key on: site/country counts, phase-months,
# site-months (progressively-ramping, not a flat n_sites x months), monitoring
# visits (on-site vs remote — see SITE_MONTH_RAMP note below), safety volume,
# subject/data volume, supply-chain shipments and the comparator-drug basis.
# All of these are ADDITIVE — nothing here replaces or edits an existing
# QTY_BASES entry, so every healthy-card qty_basis resolves exactly as before.
# ---------------------------------------------------------------------------

# Sites do not run for the full phase at 100% — they activate progressively.
# A flat `n_sites x months` would overstate site-management cost by ~40-65%.
# These three ramp constants are calibrated to reproduce Novotech's own real
# site-month counts almost exactly (178/359/136 vs. this formula's 180/360/136
# on the default patient-oncology profile). Rate-card constant, not a citation
# — see SOURCE_REGISTRY["src.kshb012_bids"].
SITE_MONTH_RAMP: dict[str, float] = {"startup": 0.75, "active": 0.60, "closeout": 0.85}


def _active_months(d: StudyDrivers) -> float:
    """Recruitment + treatment + follow-up — the "active" phase span used by
    every per-active-month line (project/medical/data/biostat management)."""
    return d.recruitment_months + d.treatment_months + d.followup_months


QTY_BASES.update({
    # --- site / country ---
    "per_site_selected":        lambda d: d.n_sites_selected,
    "per_country":              lambda d: d.n_countries,
    "per_site_year":            lambda d: max(1, round(d.n_sites * d.project_duration_months / 12)),
    "per_site_year_after_first": lambda d: max(
        0, round(d.n_sites * d.project_duration_months / 12) - d.n_sites),
    "per_site_retention_year":  lambda d: d.n_sites * d.archival_retention_years,
    "per_site_amendment":       lambda d: d.n_sites * d.n_protocol_amendments,

    # --- phase months ---
    "per_startup_month":        lambda d: d.startup_months,
    "per_recruitment_month":    lambda d: d.recruitment_months,
    "per_treatment_month":      lambda d: d.treatment_months,
    "per_followup_month":       lambda d: d.followup_months,
    "per_closeout_month":       lambda d: d.closeout_months,
    "per_active_month":         lambda d: _active_months(d),
    "per_prestartup_month":     lambda d: min(2.0, d.startup_months),
    "per_safety_month":         lambda d: max(0.0, d.project_duration_months - d.closeout_months + 1),
    "per_edc_month":            lambda d: max(0.0, d.project_duration_months - 6),
    "per_month_capped_21":      lambda d: min(21.0, math.ceil(d.project_duration_months)),

    # --- site-months (progressively ramped, not a flat n_sites x months) ---
    "per_site_startup_month":   lambda d: round(d.n_sites * d.startup_months * SITE_MONTH_RAMP["startup"]),
    "per_site_active_month":    lambda d: round(d.n_sites * _active_months(d) * SITE_MONTH_RAMP["active"]),
    "per_site_closeout_month":  lambda d: round(d.n_sites * d.closeout_months * SITE_MONTH_RAMP["closeout"]),
    "per_site_licence_month":   lambda d: round(d.n_sites * d.project_duration_months * 0.62),
    "per_site_ip_month":        lambda d: (
        round(d.n_sites * _active_months(d) * SITE_MONTH_RAMP["active"])
        + round(d.n_sites * d.closeout_months * SITE_MONTH_RAMP["closeout"])
    ),

    # --- monitoring visits (computed by the engine and stored on StudyDrivers
    # from the linked driver-resolution step; these bases just read them) ---
    "per_siv":                  lambda d: d.n_siv,
    "per_cov":                  lambda d: d.n_cov,
    "per_qualification_visit":  lambda d: d.n_qualification_visits,
    "per_imv_onsite":           lambda d: d.n_imv_onsite,
    "per_imv_remote":           lambda d: d.n_imv_remote,
    "per_pharmacy_visit":       lambda d: d.n_pharmacy_visits,
    "per_onsite_monitoring_day": lambda d: d.n_siv + d.n_imv_onsite + d.n_cov + d.n_pharmacy_visits,
    "per_monitoring_trip":      lambda d: (
        d.n_siv + d.n_imv_onsite + d.n_cov + round(d.n_pharmacy_visits * 0.35)
    ),

    # --- safety volume ---
    "per_sae":                  lambda d: d.n_expected_saes,
    "per_susar":                lambda d: d.n_expected_susars,

    # --- subject / data volume ---
    "per_completed":            lambda d: d.n_completed,
    "per_dropout":              lambda d: d.n_dropouts,
    "per_patient_visit":        lambda d: (
        d.n_visits_per_patient * d.n_completed
        + round(d.n_visits_per_patient * 0.5) * d.n_dropouts
    ),
    "per_caregiver_visit":      lambda d: round(QTY_BASES["per_patient_visit"](d) * 0.30),
    "per_crf_page":             lambda d: (
        d.crf_pages_completer * d.n_completed + d.crf_pages_dropout * d.n_dropouts
    ),
    "per_imaging_timepoint":    lambda d: (
        d.imaging_timepoints_completer * d.n_completed + d.imaging_timepoints_dropout * d.n_dropouts
    ),
    "per_amendment":            lambda d: d.n_protocol_amendments,

    # --- supply chain ---
    "per_ip_shipment":          lambda d: d.n_sites * d.ip_shipments_per_site,
    "per_depot_inbound":        lambda d: max(1, round(d.n_depots * 5)),
    "per_pallet_month":         lambda d: round(d.n_depots * 8 * d.project_duration_months),
    "per_ip_retention_year":    lambda d: 5,

    # --- comparator drug ---
    "per_comparator_patient":   lambda d: round(d.n_enrolled * d.comparator_arm_fraction),
})


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

# D2. Patient-card include predicates — additive, same rationale as C2 above.
INCLUDE_PREDICATES.update({
    "patient_profile":       lambda d: d.subject_profile == "patient",
    "oncology":              lambda d: d.therapeutic_area == "oncology",
    "bicr_required":         lambda d: d.bicr_required,
    "multi_country":         lambda d: d.n_countries > 1,
    "multi_site":            lambda d: d.n_sites > 1,
    "has_saes":              lambda d: d.n_expected_saes > 0,
    "comparator_sourced":    lambda d: d.comparator_sourced_by_sponsor,
    "has_remote_monitoring": lambda d: d.n_imv_remote > 0,
    "equipment_rented":      lambda d: d.project_duration_months < 18,
    "equipment_purchased":   lambda d: d.project_duration_months >= 18,
})


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


# ---------------------------------------------------------------------------
# F2. PATIENT_STUDY_PROFILES — the patient-card analogue of
# DEFAULT_STUDY_PROFILES above. Keyed as f"{study_type}_oncology" /
# f"{study_type}_patient" by services.budget_engine._resolve_patient_profile_key;
# only the two keys actually researched are populated (calibrated on the three
# real KSHB012-101 CRO bids — src.kshb012_bids), with "biosimilar_pk_patient"
# also serving as the engine's fallback for any study-type/therapeutic-area
# combination this rate card was not specifically researched for.
# ---------------------------------------------------------------------------

PATIENT_STUDY_PROFILES: dict[str, dict] = {
    "biosimilar_pk_oncology": dict(          # calibrated on KSHB012-101 (3 bids)
        n_enrolled=128, screen_failure_rate_pct=18.0, dropout_rate_pct=20.0,
        alternate_fraction=0.0,
        n_sites=40, n_sites_selected=34, n_countries=7,
        startup_months=6.0, recruitment_months=8.0, treatment_months=6.0,
        followup_months=1.0, closeout_months=4.0,
        n_expected_saes=60, n_expected_susars=21,
        n_visits_per_patient=16, crf_pages_completer=128, crf_pages_dropout=77,
        n_protocol_amendments=2, n_external_vendors=6, n_depots=4,
        geography_profile="global_emerging_mixed",
        archival_retention_years=15, bicr_required=True,
        imaging_timepoints_completer=4, imaging_timepoints_dropout=2,
        ip_shipments_per_site=15, complexity="complex",
    ),
    "biosimilar_pk_patient": dict(           # non-oncology patient study (fallback)
        n_enrolled=100, screen_failure_rate_pct=18.0, dropout_rate_pct=15.0,
        alternate_fraction=0.0,
        n_sites=25, n_sites_selected=22, n_countries=5,
        startup_months=5.0, recruitment_months=7.0, treatment_months=6.0,
        followup_months=1.0, closeout_months=3.0,
        n_expected_saes=24, n_expected_susars=8,
        n_visits_per_patient=12, crf_pages_completer=100, crf_pages_dropout=60,
        n_protocol_amendments=1, n_external_vendors=4, n_depots=2,
        geography_profile="global_emerging_mixed",
        archival_retention_years=15, bicr_required=False,
        imaging_timepoints_completer=0, imaging_timepoints_dropout=0,
        ip_shipments_per_site=12, complexity="complex",
    ),
}


# ---------------------------------------------------------------------------
# F3. GEOGRAPHY_FACTORS — multiplies every geo_scaled=True line (the
# investigator-grants bucket plus img.site_setup): these are US-published
# institutional rates, and a multinational patient study is priced overwhelmingly
# outside the US. CRO-bid-calibrated rates (tier "bid") are already
# global-blended by the bidding CRO and must NEVER be scaled again.
# ---------------------------------------------------------------------------

GEOGRAPHY_FACTORS: dict[str, float] = {
    "us_only":                1.00,
    "eu_west":                 0.85,
    "eu_east":                 0.45,
    "latam":                   0.45,
    "apac":                    0.40,
    "china":                   0.35,
    "india":                   0.22,
    "global_emerging_mixed":   0.45,   # DEFAULT for a multi-country patient study
    "global_developed_mixed":  0.80,
}


# ---------------------------------------------------------------------------
# F4. Comparator-drug sourcing (spec §1.6) — deliberately gated OFF
# (Budget.comparator_sourced_by_sponsor defaults False). COMPARATOR_PRICE_KB is
# seeded with the one molecule the research actually verified a real,
# manufacturer-published price for (nivolumab / OPDIVO). A drug not in the KB
# produces comparator_unit_cost() -> None, so the comp.drug line stays
# actuals_only with no invented figure.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComparatorPrice:
    usd_per_mg_us: float           # manufacturer US list price, $/mg
    usd_per_mg_ex_us: float        # planning rate for EU/ex-US sourced product, $/mg
    mg_per_patient_course: float   # total mg administered per full course
    vial_waste_factor: float       # 1.00 = flat-dose regimen, exact vial multiples
    source_id: str                 # -> SOURCE_REGISTRY


COMPARATOR_PRICE_KB: dict[str, ComparatorPrice] = {
    "nivolumab": ComparatorPrice(
        usd_per_mg_us=33.758, usd_per_mg_ex_us=14.00,
        mg_per_patient_course=6240.0,      # 240mg Q2W x26 == 480mg Q4W x13
        vial_waste_factor=1.00,
        source_id="src.bms_opdivo_list",
    ),
}

# Both factors are explicitly labelled assumptions in the research, not
# published rates — see PATIENT_RATE_CARD's comp.* line notes.
COMPARATOR_EXPOSURE_FACTOR = 0.80   # mean treatment-exposure fraction of the full course
COMPARATOR_OVERAGE_FACTOR = 0.30    # Tufts/N-SIDE: 25-40% of purchased comparator goes unused
COMPARATOR_AGENT_FEE_PCT = 12.0     # UNVERIFIED placeholder — no sourcing agent publishes a markup


def comparator_unit_cost(d: StudyDrivers) -> float | None:
    """US-sourced, fully-loaded per-patient comparator acquisition cost:
    unit price x course mg x vial-waste x exposure x (1 + overage). Returns
    None when the drug is not in COMPARATOR_PRICE_KB — the caller must not
    substitute a guess."""
    cp = COMPARATOR_PRICE_KB.get((d.drug_inn or "").strip().lower())
    if cp is None:
        return None
    return round(
        cp.usd_per_mg_us * cp.mg_per_patient_course * cp.vial_waste_factor
        * COMPARATOR_EXPOSURE_FACTOR * (1.0 + COMPARATOR_OVERAGE_FACTOR),
        2,
    )


def advertising_cost(d: StudyDrivers) -> float:
    """Scales with planned screened-subject count, clamped to the observed
    $30k-$60k band across the reference documents."""
    return float(min(60000, max(30000, round(d.n_screened * 350, -3))))


def unit_cost_for(line: RateCardLine, d: StudyDrivers) -> float | None:
    if line.key == "recruiting.advertising":
        return advertising_cost(d)

    # -- Patient-card special cases (spec §3.5/§1.6/§2.2 C19) ----------------
    if line.key == "comp.drug":
        return comparator_unit_cost(d)
    if line.key == "comp.agent_fee":
        # % of the comp.drug line's TOTAL acquisition value, not a per-unit rate.
        drug_unit = comparator_unit_cost(d)
        if drug_unit is None:
            return None
        qty = QTY_BASES["per_comparator_patient"](d)
        return round(drug_unit * qty * COMPARATOR_AGENT_FEE_PCT / 100.0, 2)
    if line.key == "comp.qp_release":
        # No public figure exists for import/QP-release/re-labelling/blinding
        # cost — emitted not_planned with no invented number (spec §2.2 C18).
        return None
    if line.key == "anc.equipment":
        # Rental vs purchase crossover at ~18 months on-site (spec §2.2 C19 /
        # RECOMMENDED RATES §1): rental build-up is freezer 1,000 + centrifuge
        # 542 + infusion pump 225 = 1,767/site-month (src.kwipped_rentals);
        # purchase is a flat 26,000/site (src.usalab_transcat + institutional
        # site-fee schedules, geo-unscaled since it is a global commodity cost).
        if d.project_duration_months < 18:
            return round(1767.0 * d.project_duration_months, 2)
        return 26000.0

    base = (line.unit_cost_complex if d.complexity == "complex"
            and line.unit_cost_complex is not None else line.unit_cost)
    if base is not None and line.geo_scaled and d.geography_factor != 1.0:
        return round(base * d.geography_factor, 2)
    return base


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

# Backward-compatible alias — HEALTHY_RATE_CARD is the name this module's own
# docstring and the patient-card comments use throughout; RATE_CARD is kept as
# the primary name because it is what every pre-existing importer
# (services/budget_engine.py, services/budget_docx_export.py) already uses.
# Byte-identical object, not a copy.
HEALTHY_RATE_CARD: tuple[RateCardCategory, ...] = RATE_CARD


# ---------------------------------------------------------------------------
# J. Provenance — RateCardSource / PROVENANCE_TIERS / SOURCE_REGISTRY
#
# Distinct from BudgetLineSource (models.py), which is a coarse UI badge.
# This is the actual citation: where a researched unit cost came from, at
# what confidence tier, and — critically — what it is NOT (the caveat), so a
# real published figure is never silently over-claimed as more general than
# the research found it to be. See spec §4.3.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateCardSource:
    """A real, citable external figure. Distinct from an approximation."""
    id: str                      # stable key, e.g. "src.emory_2025"
    tier: str                    # key into PROVENANCE_TIERS
    figure: str                  # the figure AS PUBLISHED
    citation: str                # full human-readable citation
    url: str | None
    accessed: str                # "YYYY-MM"
    caveat: str | None = None    # what the figure is NOT, in plain language


PROVENANCE_TIERS: dict[str, str] = {
    "published_rate": (
        "Published rate — read directly from an official fee schedule, "
        "manufacturer price list, government schedule or peer-reviewed benchmark."
    ),
    "derived": (
        "Derived — triangulated by us from two or more published figures. Not "
        "itself a published benchmark."
    ),
    "bid_calibrated": (
        "Bid-calibrated — a unit rate taken from a real CRO bid for a "
        "comparable study. A single vendor's price, not a market benchmark."
    ),
    "estimate": (
        "Estimate — a rate-card approximation. No public figure exists for this "
        "item; treat it as an assumption to be replaced by a quote."
    ),
}

SOURCE_REGISTRY: dict[str, RateCardSource] = {
    "src.emory_2025": RateCardSource(
        id="src.emory_2025", tier="published_rate",
        figure="Start-up 12,150 + budget development 8,100 + coverage analysis 6,750 "
               "= ~27,000; IRB submission 6,075, renewal 3,375, amendment 1,350; "
               "RECIST response-criteria assessment 540 per timepoint; radiology "
               "processing 1,500/study; SAE/UADE/UPIRSO 475 each; IND safety report "
               "50 each; reconsent 270 per participant; study closeout 4,050; record "
               "retention/storage 675 ANNUALLY; monitoring 150/hour.",
        citation="Emory Clinical Research Office, \"Emory University Standard Research "
                 "Fees for Industry-Sponsored and/or Funded Studies,\" memorandum dated "
                 "10 July 2025 (Winship Cancer Institute fee schedule).",
        url="https://ocr.emory.edu/_includes/documents/emory-2025-industry-funded-study-fees.pdf",
        accessed="2026-09",
        caveat="US NCI-designated cancer-centre rate with 35% F&A already applied. "
               "Scaled by the study's geography_factor for non-US sites.",
    ),
    "src.utah_fy2027": RateCardSource(
        id="src.utah_fy2027", tier="published_rate",
        figure="Site monitoring-visit hosting: 918/day on-site, 1,350/event remote; "
               "record retention 675/year (invoices 15 years at close-out if the "
               "contract is silent); CTO maintenance 10,000/yr + PI oversight 7,500/yr "
               "+ institutional oversight 32,760/yr; site closeout ~6,075 all-in.",
        citation="University of Utah Research Quality and Compliance, UUSOP-13 "
                 "Clinical Research Institutional Fee Schedule, version dated 1 July "
                 "2026 (FY2027).",
        url="https://qualitycompliance.research.utah.edu/sop-library/uu-sop-13.php",
        accessed="2026-09",
        caveat="The 15-year default archiving assumption in this card comes directly "
               "from Utah's stated policy, not from any other schedule.",
    ),
    "src.kumc_2023": RateCardSource(
        id="src.kumc_2023", tier="published_rate",
        figure="Start-up Tier 1 (interventional/drug) 15,650; annual administrative "
               "maintenance 4,200/year after year 1; SAE reporting 650/event; "
               "archiving 560 one-time (subject to 33.5% overhead).",
        citation="KU Medical Center Research Institute, \"Fees for Industry-Sponsored "
                 "Clinical Research,\" updated 10 January 2023.",
        url="https://www.kumc.edu/documents/research-administration/KUMCRI%20Fee%20Schedule.pdf",
        accessed="2026-09",
        caveat="2023 dollars, not escalated — no verifiable current-year clinical-site "
               "cost-inflation index was found to escalate this specific figure, so it "
               "is used as-is (a conservative choice, since costs are more likely to "
               "have risen than fallen since 2023).",
    ),
    "src.wcg_irb_2026": RateCardSource(
        id="src.wcg_irb_2026", tier="published_rate",
        figure="Initial study review 1,994 (protocol-level, once); PI/site review "
               "1,646 (2,194 if custom consent); continuing review of PI 1,646/year; "
               "protocol amendment 790 per PI (1,056 with ICF revision); study close "
               "out 442 (study-level) + 372 per PI.",
        citation="WCG IRB, \"2026 Standard Fee Schedule,\" as posted by Boston "
                 "University Medical Campus IRB.",
        url="https://www.bumc.bu.edu/irb/files/2026/01/WCG-IRB-Standard-Fee-Schedule.pdf",
        accessed="2026-09",
        caveat="Central-IRB fee only — ADDITIONAL to the local institutional context "
               "review a central-IRB study still incurs (folded into ig.irb_year1).",
    ),
    "src.jop_2007_visits": RateCardSource(
        id="src.jop_2007_visits", tier="published_rate",
        figure="Screening visit 1,170; minor treatment cycle 1,005; major treatment "
               "cycle 1,497; follow-up visit 387; screen failure paid at the "
               "screening-visit rate; study start-up 9,289.",
        citation="\"Developing an Investigator Site Budget for Clinical Trials,\" "
                 "Journal of Oncology Practice, Vol 3, Issue 2 (2007).",
        url="https://pmc.ncbi.nlm.nih.gov/articles/PMC2793732/",
        accessed="2026-09",
        caveat="2007 dollars — the only itemised per-visit oncology site budget found "
               "in the open literature. The STRUCTURE is used and escalated ~2.0-2.5x "
               "to 2026; the raw figures are stale.",
    ),
    "src.ims_oncology_grant": RateCardSource(
        id="src.ims_oncology_grant", tier="published_rate",
        figure="18,654 total negotiated investigator cost per patient, oncology "
               "(vs 7,458 cardiovascular).",
        citation="Clinical Leader, \"Investigator Payments — Channels, Cycle Time and "
                 "Benchmarks,\" attributing IMS Health.",
        url="https://www.clinicaltrialleader.com/doc/investigator-payments-channels-cycle-time-and-benchmarks-0001",
        accessed="2026-09",
        caveat="Year not stated in the source; inferred circa 2012-2013 from context. "
               "Escalated to 2026 using the same ~2.0-2.5x multiplier applied to "
               "src.jop_2007_visits elsewhere in this card, for internal consistency "
               "between the two independent legacy benchmarks (no oncology-specific "
               "cost-inflation index for this exact period was found).",
    ),
    "src.alcoa_crio_2025": RateCardSource(
        id="src.alcoa_crio_2025", tier="published_rate",
        figure="On-site IMV 4,500 CRA fee + 1,000 travel; remote IMV 2,500, no "
               "travel; EDC licensing 230,000 for a 20-site/300-subject/28-month "
               "study (=411/site-month); EDC database build/UAT 120,000 (separate "
               "from licensing); EDC site user support 23,100/20 sites (=1,155/site).",
        citation="Goldman R. & Rudolph J. (ALCOA Consulting LLC), \"How Sponsors Can "
                 "Leverage New Technologies To Lower Trial Costs,\" CRIO case study, "
                 "April 2025.",
        url="https://clinicalresearch.io/wp-content/uploads/2025/04/How-Sponsors-Can-Leverage-New-Technologies-To-Lower-Trial-Costs.pdf",
        accessed="2026-09",
        caveat="US-centric, non-oncology, Phase 2, 20 sites, 300 subjects, 28 months. "
               "The 2,400-4,960 range in a real CRO bid is very likely a "
               "remote-vs-on-site blend of these two figures, not a geography range.",
    ),
    "src.jmir_2023_remote": RateCardSource(
        id="src.jmir_2023_remote", tier="published_rate",
        figure="46.2% reduction in total monitoring cost, hybrid vs 100% on-site.",
        citation="\"Reducing Clinical Trial Monitoring Resources and Costs With Remote "
                 "Monitoring: Retrospective Study Comparing On-Site Versus Hybrid "
                 "Monitoring,\" J Med Internet Res 2023;25:e42175.",
        url="https://www.jmir.org/2023/1/e42175",
        accessed="2026-09",
        caveat="Peer-reviewed percentage is transferable; the underlying absolute "
               "China-only travel/accommodation figures are not.",
    ),
    "src.erg_aspe_2014": RateCardSource(
        id="src.erg_aspe_2014", tier="published_rate",
        figure="Central laboratory: 849,180/study (Phase 3), 804,821/study (Phase 2), "
               "4-12% of per-study subtotal by phase.",
        citation="Sertkaya A, Birkenbach A, Berlind A, Eyraud J (Eastern Research "
                 "Group) for US HHS/ASPE, \"Examination of Clinical Trial Costs and "
                 "Barriers for Drug Development,\" 25 July 2014, Table 2.",
        url="https://aspe.hhs.gov/sites/default/files/private/pdf/77166/rpt_erg.pdf",
        accessed="2026-09",
        caveat="Weighted average across ALL 13 therapeutic areas, 2004-2012 contract "
               "data — there is no oncology-specific central-lab row in this source. "
               "Central lab is a PER-PATIENT unit of measure in the underlying "
               "Medidata PICAS data, which is why lab.analysis/lab.setup here are "
               "modelled as fixed-plus-variable rather than a flat per-study figure.",
    ),
    "src.tufts_2022": RateCardSource(
        id="src.tufts_2022", tier="published_rate",
        figure="24.8 visits/patient (Phase II oncology), 30.0 (Phase III); mean trial "
               "budget per enrolled patient: Phase I 180,862, Phase II 246,007, "
               "Phase III 183,833 (TOTAL trial cost, not a site grant).",
        citation="Tufts Center for the Study of Drug Development, Impact Report Vol "
                 "24 No 2, March/April 2022, \"Cost variation and mis-estimation "
                 "characterize clinical trial budgets, particularly in early phases.\"",
        url="https://www.clinicaltrialvanguard.com/wp-content/uploads/2024/08/Mar-Apr-2022-Clinical-Trial-Budgets.pdf",
        accessed="2026-09",
        caveat="The 246,007 Phase II oncology figure is TOTAL trial cost including "
               "CRO/vendor spend — not an investigator-grant figure. Misreading it as "
               "a site payment overstates by roughly 10x.",
    ),
    "src.iowa_radcore": RateCardSource(
        id="src.iowa_radcore", tier="published_rate",
        figure="Radiology start-up 2,142/study; annual fee 918/year; phantom scan "
               "2,500 (when protocol-indicated); retrospective image transfer 75-247.50.",
        citation="University of Iowa Radiology Core Lab, \"Services and Rates,\" "
                 "FY25-FY26 rates.",
        url="https://radiologycore.lab.uiowa.edu/services-and-rates",
        accessed="2026-09",
        caveat="Site-side imaging administration only; does not include the scan "
               "itself or a BICR read.",
    ),
    "src.median_2024": RateCardSource(
        id="src.median_2024", tier="derived",
        figure="EUR 3.1 million total for TWO large oncology Phase III imaging "
               "studies (~EUR 1.55m / ~USD 1.68m per study).",
        citation="Median Technologies SA (Euronext Growth: ALMDT), Half-Year "
                 "Financial Report as of 30 June 2024, p.17.",
        url="https://mediantechnologies.com/wp-content/uploads/2024/10/En_MEDIAN-FinancialReport_06-30-2024.pdf",
        accessed="2026-09",
        caveat="A CONTRACT TOTAL from a regulated financial filing, not a published "
               "per-patient or per-timepoint rate. The 700/timepoint figure here is "
               "derived by dividing that contract value across a plausible 500-800 "
               "randomised patients at ~24-30 timepoints each — this is the only real "
               "vendor-side BICR money figure found anywhere; no imaging CRO "
               "publishes per-read pricing.",
    ),
    "src.bms_opdivo_list": RateCardSource(
        id="src.bms_opdivo_list", tier="published_rate",
        figure="7,943.08 per 240 mg vial = 33.096 USD/mg (Oct-2025), linear across "
               "all four vial presentations; +2.0% Jan-2026 list move -> 33.758 USD/mg.",
        citation="Bristol Myers Squibb official list-price disclosure, "
                 "bmspricinginformation.com/opdivo/ (footer document code 10/25).",
        url="https://www.bmspricinginformation.com/opdivo/",
        accessed="2026-09",
        caveat="Manufacturer's own US list price — the strongest citation available. "
               "Medicare ASP+6% (34.222 USD/mg, Q3 2026) is a REIMBURSEMENT rate, "
               "never an acquisition price, and must not be used here.",
    ),
    "src.tufts_comparator_waste": RateCardSource(
        id="src.tufts_comparator_waste", tier="published_rate",
        figure="Up to 40% of comparator drug supply goes unused (30-55% in the older "
               "2013 analysis); a modelled oncology trial went from 65% projected "
               "waste to 35% after optimisation.",
        citation="Lamberti MJ & Getz K (Tufts CSDD), \"Tracking Trial Cost Drivers: "
                 "The Impact of Comparator Drugs and Co-Therapies,\" Pharmaceutical "
                 "Executive, 1 May 2013; corroborated by Myonex (citing Tufts CSDD "
                 "2024) and an N-SIDE 2025 oncology case study.",
        url="https://www.n-side.com/en/insights/optimizing-comparator-drug-sourcing-for-safe-and-efficient-clinical-supply/",
        accessed="2026-09",
        caveat="30% is a mid-planning figure for a well-managed sourcing programme, "
               "not a guaranteed rate; exposed as an editable assumption "
               "(COMPARATOR_OVERAGE_FACTOR).",
    ),
    "src.hopkins_fy2024": RateCardSource(
        id="src.hopkins_fy2024", tier="published_rate",
        figure="Clinical Engineering annual maintenance up to 737/year (sponsor "
               "equipment); extended archiving fee 737/additional year; monthly drug "
               "storage up to 170/month; shipping to other facility up to 317; "
               "long-term record retention 2,211 one-time (minimum 7 years).",
        citation="Johns Hopkins University Standard Costs and Fees FY2024 v2, "
                 "4 August 2023.",
        url="https://www.hopkinsmedicine.org/-/media/research/documents/offices-policies/crss-standard-costs-and-fees-fy2024-v2-04-august-2023.pdf",
        accessed="2026-09",
        caveat="US academic medical centre rate; scaled by geography_factor for "
               "non-US sites.",
    ),
    "src.morehouse_fy2025": RateCardSource(
        id="src.morehouse_fy2025", tier="published_rate",
        figure="Pharmacy/IDS set-up 3,000 simple / 4,500 complex; site closeout 1,500 "
               "(~20 hours effort); record retention 1,500 one-time (minimum 7 years).",
        citation="Morehouse School of Medicine Clinical Trials Office, Standard Costs "
                 "and Fees FY2025 (effective 7/1/2024).",
        url="https://researchwebportal.msm.edu/wp-content/uploads/2025/01/CTO-Clinical-Trials-Fee-Schedule.pdf",
        accessed="2026-09",
        caveat="US academic medical centre rate; scaled by geography_factor for "
               "non-US sites.",
    ),
    "src.kwipped_rentals": RateCardSource(
        id="src.kwipped_rentals", tier="published_rate",
        figure="Laboratory freezer rental 1,000/month (range 308-2,674); refrigerated "
               "centrifuge 542/month (range 17-2,721); infusion pump 225/month "
               "(range 40-999) — averages of actual supplier quotes over the "
               "preceding 12 months.",
        citation="KWIPPED equipment rental marketplace, Laboratory Freezers / "
                 "Laboratory Centrifuges / Infusion Pumps category pages.",
        url="https://www.kwipped.com/rentals/laboratory/laboratory-freezers/439",
        accessed="2026-09",
        caveat="A GENERAL lab-equipment marketplace, not a GCP-qualified "
               "clinical-trial supplier — a floor, not a market rate. Clinical-trial "
               "suppliers bundle calibration certificates, IQ/OQ, customs and "
               "end-of-study recovery, which are not reflected in this floor.",
    ),
    "src.usalab_transcat": RateCardSource(
        id="src.usalab_transcat", tier="published_rate",
        figure="ULT -86C freezer purchase 9,000-14,000 (typical 10-25 cu ft site "
               "unit); refrigerated benchtop centrifuge (Eppendorf 5810 R) "
               "18,053-19,003 new.",
        citation="USA Lab published ULT freezer pricing; Transcat published Eppendorf "
                 "Centrifuge 5810 R pricing.",
        url="https://www.usalab.com/freezers/",
        accessed="2026-09",
        caveat="Live retail catalogue prices, not a clinical-trial vendor quote; "
               "excludes shipping, installation, validation and racking.",
    ),
    "src.warehousing_2026": RateCardSource(
        id="src.warehousing_2026", tier="published_rate",
        figure="cGMP refrigerated (2-8C) depot storage 58-135/pallet/month; ambient "
               "32-58/pallet/month; -20C 85-185/pallet/month; -80C 240-520/RACK "
               "(not pallet)/month; cold-chain qualified shipper handling 45-320/shipment.",
        citation="WarehousingCosts.com, \"Pharmaceutical Warehousing Costs (2026): GMP "
                 "Storage Rates, Cold Chain Pricing & DEA Vault Costs.\"",
        url="https://warehousingcosts.com/guides/pharmaceutical-warehousing-costs",
        accessed="2026-09",
        caveat="A specialist pricing-guide publisher, not a vendor rate card or "
               "peer-reviewed source. -80C is priced per RACK-equivalent, not per "
               "pallet — applying a pallet basis at -80C understates by ~10x.",
    ),
    "src.sofpromed_quote": RateCardSource(
        id="src.sofpromed_quote", tier="published_rate",
        figure="Real vendor quotation: outbound ambient shipment 350; inbound depot "
               "receipt 500; depot project management ~694/month (25,000/36 months); "
               "labelling 4.00/bottle.",
        citation="Sofpromed, \"What is the Cost of Packaging, Labeling, Storing, and "
                 "Distributing Drugs in a Clinical Trial?\", reproducing an actual "
                 "vendor quotation.",
        url="https://www.sofpromed.com/what-is-the-cost-of-packaging-labeling-storing-and-distributing-drugs-in-a-clinical-trial",
        accessed="2026-09",
        caveat="Ambient (not cold-chain), 6 sites, single-country scale — a real "
               "quotation, not a market survey; scaled here to a 40-site cold-chain "
               "multinational programme.",
    ),
    "src.suvoda_rtsm_2026": RateCardSource(
        id="src.suvoda_rtsm_2026", tier="published_rate",
        figure="RTSM/IRT typically represents 2-3% of the total study budget for "
               "Phase II and III trials.",
        citation="Suvoda, \"RTSM (IRT) FAQ: A guide to Randomization and Trial Supply "
                 "Management technology for clinical trials,\" 23 February 2026.",
        url="https://www.suvoda.com/insights/blog/rtsm-irt-faq-a-guide-to-interactive-response-technology",
        accessed="2026-09",
        caveat="Vendor-published, but a specific checkable claim from a market "
               "leader; no RTSM vendor publishes dollar unit prices.",
    ),
    "src.fda_biosimilar_2025": RateCardSource(
        id="src.fda_biosimilar_2025", tier="published_rate",
        figure="~24 million average per comparative-efficacy study, 1-3 years; FDA "
               "estimates ~100 million saved per biosimilar programme where the "
               "study is no longer required.",
        citation="US FDA, \"FDA Moves to Accelerate Biosimilar Development and Lower "
                 "Drug Costs,\" press announcement, 29 October 2025.",
        url="https://www.fda.gov/news-events/press-announcements",
        accessed="2026-09",
        caveat="As of 29 Oct 2025, FDA's draft guidance says comparative-efficacy "
               "studies are generally no longer needed for biosimilars — a "
               "PK-similarity study (this card's default) still requires comparator; "
               "a large comparative-efficacy study may not.",
    ),
    "src.kshb012_bids": RateCardSource(
        id="src.kshb012_bids", tier="bid_calibrated",
        figure="Grand totals: Novotech 11,701,368 USD; PPD/Emmes 13,701,349 USD "
               "(after 8% discount); IQVIA region-stratified, comparable.",
        citation="Three real 2026 CRO bids for Kashiv Biosciences KSHB012-101, a "
                 "nivolumab (OPDIVO) biosimilar study in melanoma/oesophageal cancer "
                 "patients, Phase I-II, randomized double-blind active-controlled "
                 "parallel-group (Novotech, IQVIA, PPD/Emmes).",
        url=None, accessed="2026-09",
        caveat="A single sponsor's real bid set, not an industry benchmark database. "
               "Every tier='bid' unit rate in this card is taken directly from one "
               "of these three documents.",
    ),
    "src.neoteryx_shipping": RateCardSource(
        id="src.neoteryx_shipping", tier="published_rate",
        figure="Frozen blood sample shipment (dry ice): 178.62 domestic US average, "
               "482.51 international average (FedEx Overnight Priority, 2012 "
               "bioMontr study).",
        citation="Neoteryx, \"How to Reduce Costs of Shipping Blood Samples,\" citing "
                 "a 2012 bioMontr study.",
        url="https://www.neoteryx.com/microsampling-blog/how-to-reduce-costs-of-shipping-blood-samples",
        accessed="2026-09",
        caveat="2012 vintage, read via a secondary summary (Neoteryx sells a "
               "competing microsampling product). Inflated ~35% here for 2026 "
               "planning; treat as indicative, not authoritative.",
    ),
    "src.georgia_ctsa": RateCardSource(
        id="src.georgia_ctsa", tier="published_rate",
        figure="Serum/plasma processing 19/unit; PBMC 42/unit; lab usage & equipment "
               "fee 21/visit; sample storage 35/month or 420/year.",
        citation="Georgia CTSA (Emory University), GCRC Fee Schedule, updated 23 "
                 "August 2023, \"Laboratory Services / Sample Processing Charges.\"",
        url="https://georgiactsa.org/_includes/documents/sections/research/gcrcs/updated-georgia-ctsa-gcrc-fee-schedule-08-23-2023.pdf",
        accessed="2026-09",
        caveat="Site-side sample handling charges to the sponsor; a genuine, current, "
               "published price list.",
    ),
    "src.careroute_ct74177": RateCardSource(
        id="src.careroute_ct74177", tier="published_rate",
        figure="CT abdomen/pelvis with contrast (CPT 74177): Medicare 2026 global "
               "rate 300.27; average provider charge 1,049.94; commercial payer "
               "averages 380-487; hospital outpatient 1,500-3,000.",
        citation="CareRoute CPT 74177 cost page (2026 Medicare rate, charge and cash "
                 "data); PayerPrice CPT 74177 fee schedule (commercial payer "
                 "averages).",
        url="https://www.careroute.ai/costs/cpt/74177",
        accessed="2026-09",
        caveat="Covers abdomen+pelvis only — oncology RECIST imaging is normally "
               "chest+abdomen+pelvis, so a verified chest-CT add-on rate was NOT "
               "found; this line is a lower-bound approximation for the full "
               "three-region scan and defaults to not_planned (the scan is normally "
               "inside the per-patient investigator grant).",
    ),
    "src.withnell_sensors": RateCardSource(
        id="src.withnell_sensors", tier="published_rate",
        figure="Continuous/reusable temperature-monitoring logger rental from "
               "GBP 14.50/logger/week (~USD 19).",
        citation="Withnell Sensors, ULT Freezer Rental page, quoting SenseAnywhere "
                 "data loggers.",
        url="https://www.withnellsensors.co.uk/services/rental/ult-freezer-rental/",
        accessed="2026-09",
        caveat="The only published continuous-monitoring rental rate found; a site "
               "typically needs 2-4 monitored locations, which is folded into the "
               "per-site-month rate here.",
    ),
    "src.testo_logger": RateCardSource(
        id="src.testo_logger", tier="published_rate",
        figure="Single-use transport temperature data logger (Testo 184 T1, 90-day "
               "USB/PDF): 84.15-99.00 each.",
        citation="Retailer listings for Testo 184 T1 (0572 1841 01): Test Equipment "
                 "Depot and MicroDAQ.",
        url=None, accessed="2026-09",
        caveat="Honesty flag: both retailer pages could not be opened directly "
               "(HTTP 405/403) — these figures come from search-result snippets, not "
               "a page read end-to-end. Treat as indicative.",
    ),
    "src.derived_investigator_grant_per_patient": RateCardSource(
        id="src.derived_investigator_grant_per_patient", tier="derived",
        figure="36,000 USD per enrolled patient (model range 30,000-47,000) — a "
               "reasoned triangulation of two independent published benchmarks, not "
               "itself a published benchmark.",
        citation="Triangulation of src.jop_2007_visits (2007 per-visit structure, "
                 "summed and escalated ~2.0-2.5x to 2026) and src.ims_oncology_grant "
                 "(circa 2012 IMS figure of 18,654/patient, escalated by the same "
                 "~2.0-2.5x multiplier to ~37,300-46,600 for consistency between the "
                 "two legacy benchmarks). 36,000 sits at the low end of both escalated "
                 "ranges, a deliberately conservative choice.",
        url=None, accessed="2026-09",
        caveat="No current, itemised, Phase II oncology per-patient investigator-grant "
               "benchmark was found in a source that could be independently verified; "
               "both inputs to this triangulation are 13-19 years old, escalated by an "
               "assumed multiplier rather than a published inflation index. This "
               "figure must be labelled a derived estimate, never cited as a "
               "benchmark on its own — a real CRO quote should replace it before this "
               "budget is used for real financial planning. (An earlier version of "
               "this card cited a 2026 ASCO/J Clin Oncol abstract as a third, more "
               "current input; that citation could not be verified to exist anywhere "
               "on re-check and has been removed rather than left in place.) "
               "SUPERSEDED: a real CRO quote for this exact figure has since been "
               "found (Novotech's KSHB012-101 bid, 21,882.81/patient) — see "
               "ig.per_patient, which now cites src.kshb012_bids directly. This "
               "entry is kept only as a record of the prior (weaker) reasoning, "
               "not as an active source for any line item.",
    ),
    "src.derived_screen_failure_grant": RateCardSource(
        id="src.derived_screen_failure_grant", tier="derived",
        figure="2,600 USD per screen-failure patient, paid at the escalated "
               "screening-visit rate.",
        citation="Derived from src.jop_2007_visits' screening-visit rate (1,170 in "
                 "2007), escalated ~2.0-2.5x to 2026, applying Emory's stated policy "
                 "(src.emory_2025) of paying screen failures at the full negotiated "
                 "screening-visit total.",
        url=None, accessed="2026-09",
        caveat="A derivation, not a published screen-failure-specific rate. "
               "SUPERSEDED: Novotech's KSHB012-101 bid has a real rate for this "
               "exact item (PG01, 1,778.57/screen-fail patient) — see "
               "ig.screen_failure, which now cites src.kshb012_bids directly. Kept "
               "only as a record of the prior reasoning.",
    ),
    "src.internal_estimate": RateCardSource(
        id="src.internal_estimate", tier="estimate",
        figure="No public figure exists for this item.",
        citation="No vendor, institutional fee schedule, government source or "
                 "peer-reviewed benchmark was found for this line despite a targeted "
                 "search (see, e.g., the research's dedicated searches for lab-kit "
                 "unit prices, comparator-sourcing-agent markup percentages, and "
                 "sponsor-side eTMF/GxP archival pricing — all came back empty).",
        url=None, accessed="2026-09",
        caveat="Explicitly an approximation; replace with a vendor quote before "
               "relying on it for a real budget.",
    ),
}


# ---------------------------------------------------------------------------
# K. PATIENT_RATE_CARD — 22 categories for multi-site, multi-country PATIENT
# (not healthy-volunteer) studies. See module docstring and spec §2 for the
# full category-by-category rationale. Every dollar figure here is either:
#   tier "bid"  -> src.kshb012_bids (one CRO's unit rate for this exact study)
#   tier "pub"  -> a real, cited, externally-published rate (SOURCE_REGISTRY)
#   tier "der"  -> a documented triangulation of published figures
#   tier "est"  -> src.internal_estimate (no public figure exists; flagged)
# ---------------------------------------------------------------------------

PATIENT_RATE_CARD: tuple[RateCardCategory, ...] = (
    # -- C1: Medical Writing and Protocol -------------------------------------
    RateCardCategory(
        name="Medical Writing and Protocol", bucket=BudgetBucket.direct, sort_order=1,
        lines=(
            RateCardLine(
                key="mw.protocol_synopsis", label="Protocol synopsis review",
                unit_description="Per study", qty_basis="per_study", unit_cost=3408.0,
                source_ref="src.kshb012_bids",
                basis="Novotech bid unit rate for this exact study (KSHB012-101).",
            ),
            RateCardLine(
                key="mw.protocol_write", label="Protocol writing, QC and finalisation",
                unit_description="Per protocol", qty_basis="per_study", unit_cost=48855.0,
                source_ref="src.kshb012_bids",
                basis="Novotech bid unit rate for this exact study.",
            ),
            RateCardLine(
                key="mw.amendments", label="Protocol amendment authoring (CRO)",
                unit_description="per amendment", qty_basis="per_amendment", unit_cost=23594.0,
                source_ref="src.kshb012_bids",
                basis="Novotech bid regulatory/EC amendment-submission rate, reused "
                      "as the CRO-side amendment-authoring cost.",
            ),
            RateCardLine(
                key="mw.icf_master", label="Master ICF development",
                unit_description="Per study", qty_basis="per_study", unit_cost=12000.0,
                source_ref="src.internal_estimate",
                basis="No public figure found for master-ICF authoring effort on a "
                      "multi-country oncology study; rate-card approximation.",
            ),
            RateCardLine(
                key="mw.dsur_tlf", label="TLFs and narrative for DSUR",
                unit_description="per year", qty_basis="per_year", unit_cost=9056.0,
                source_ref="src.kshb012_bids",
                basis="Novotech bid unit rate.",
            ),
        ),
    ),
    # -- C2: Regulatory and Ethics ---------------------------------------------
    RateCardCategory(
        name="Regulatory and Ethics", bucket=BudgetBucket.direct, sort_order=2,
        lines=(
            RateCardLine(
                key="reg.ec_submission", label="IRB/EC submission preparation",
                unit_description="per site", qty_basis="per_site_selected", unit_cost=2704.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="reg.ha_submission", label="Competent Authority / regulatory submission",
                unit_description="per country", qty_basis="per_country", unit_cost=6471.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="reg.icf_site_prep", label="Site-specific ICF preparation",
                unit_description="per site", qty_basis="per_site_selected", unit_cost=2020.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="reg.doc_translation", label="Essential document translation management",
                unit_description="Per study", qty_basis="per_study", unit_cost=47916.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="reg.import_export", label="Import/export licence management",
                unit_description="Per study", qty_basis="per_study", unit_cost=16366.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="reg.local_sponsor", label="Act as local sponsor / legal representative",
                unit_description="per country", qty_basis="per_country", unit_cost=4618.0,
                source_ref="src.kshb012_bids",
                basis="Novotech's flat 32,328 per-study 'Act as Local Sponsor' fee, "
                      "reallocated per country (32,328 / 7 countries).",
            ),
            RateCardLine(
                key="reg.oversight_startup", label="Regulatory study-management oversight (start-up)",
                unit_description="per start-up month", qty_basis="per_startup_month", unit_cost=19869.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="reg.oversight_active", label="Regulatory oversight (recruitment/treatment/FU)",
                unit_description="per active month", qty_basis="per_active_month", unit_cost=4265.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="reg.amendment_submissions", label="Amendment regulatory and EC submissions",
                unit_description="per amendment", qty_basis="per_amendment", unit_cost=23594.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
        ),
    ),
    # -- C3: Start-Up Activities ------------------------------------------------
    RateCardCategory(
        name="Start-Up Activities", bucket=BudgetBucket.direct, sort_order=3,
        lines=(
            RateCardLine(
                key="su.feasibility", label="Country and site feasibility",
                unit_description="Per study", qty_basis="per_study", unit_cost=22710.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="su.kickoff", label="Kick-off meeting",
                unit_description="Per study", qty_basis="per_study", unit_cost=22552.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="su.team_training", label="Study team training",
                unit_description="Per study", qty_basis="per_study", unit_cost=30710.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="su.cra_training", label="CRA / investigator training meeting",
                unit_description="Per study", qty_basis="per_study", unit_cost=31931.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="su.plans", label="Project and operational plans",
                unit_description="Per study", qty_basis="per_study", unit_cost=21072.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="su.qualification_visits", label="Site qualification visits",
                unit_description="per QV", qty_basis="per_qualification_visit", unit_cost=2362.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="su.contracts", label="Site contract and budget negotiation",
                unit_description="per site", qty_basis="per_site_selected", unit_cost=6732.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="su.site_mgmt_startup", label="Site management (start-up)",
                unit_description="per site-month", qty_basis="per_site_startup_month", unit_cost=2392.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="su.vendor_id", label="Vendor identification and qualification",
                unit_description="per vendor", qty_basis="per_vendor", unit_cost=1246.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
        ),
    ),
    # -- C4: Clinical Site Management and Monitoring -----------------------------
    RateCardCategory(
        name="Clinical Site Management and Monitoring", bucket=BudgetBucket.direct, sort_order=4,
        lines=(
            RateCardLine(
                key="mon.siv", label="Site initiation visits",
                unit_description="per SIV", qty_basis="per_siv", unit_cost=2353.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="mon.imv_onsite", label="Interim monitoring visits (on-site)",
                unit_description="per on-site IMV", qty_basis="per_imv_onsite", unit_cost=4500.0,
                source_ref="src.alcoa_crio_2025",
                basis="4,500 CRA fee, published 2025 US benchmark. On-site/remote "
                      "split is the key structural correction: a bid quoting "
                      "'2,400-4,960 per visit' is quoting a remote-vs-on-site mix, "
                      "not a geography range.",
            ),
            RateCardLine(
                key="mon.imv_remote", label="Interim monitoring visits (remote)",
                unit_description="per remote IMV", qty_basis="per_imv_remote", unit_cost=2500.0,
                source_ref="src.alcoa_crio_2025",
                basis="Published 2025 US benchmark: remote IMVs modelled at 2,500 "
                      "with no travel.",
            ),
            RateCardLine(
                key="mon.pharmacy_visits", label="Unblinded pharmacy / drug-accountability visits",
                unit_description="per visit", qty_basis="per_pharmacy_visit", unit_cost=2402.0,
                source_ref="src.kshb012_bids", basis="Bid-calibrated unit rate.",
            ),
            RateCardLine(
                key="mon.cov", label="Site close-out visits",
                unit_description="per COV", qty_basis="per_cov", unit_cost=2669.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="mon.site_mgmt_active", label="Site management (recruitment/treatment)",
                unit_description="per site-month", qty_basis="per_site_active_month", unit_cost=1832.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="mon.site_mgmt_close", label="Site management (close-out)",
                unit_description="per site-month", qty_basis="per_site_closeout_month", unit_cost=1778.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="mon.rbqm", label="Risk-based quality management / central monitoring",
                unit_description="per active month", qty_basis="per_active_month", unit_cost=1216.0,
                source_ref="src.kshb012_bids", basis="Bid-calibrated unit rate.",
            ),
        ),
    ),
    # -- C5: Study and Project Management ---------------------------------------
    RateCardCategory(
        name="Study and Project Management", bucket=BudgetBucket.direct, sort_order=5,
        lines=(
            RateCardLine(
                key="pm.pre_startup", label="Project management (pre-start-up)",
                unit_description="per month", qty_basis="per_prestartup_month", unit_cost=1920.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pm.startup", label="Project management (start-up)",
                unit_description="per start-up month", qty_basis="per_startup_month", unit_cost=57401.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pm.active", label="Project management (recruitment/treatment/FU)",
                unit_description="per active month", qty_basis="per_active_month", unit_cost=64379.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pm.closeout", label="Project management (close-out)",
                unit_description="per close-out month", qty_basis="per_closeout_month", unit_cost=50903.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pm.oversight_startup", label="Management oversight (start-up)",
                unit_description="per start-up month", qty_basis="per_startup_month", unit_cost=10058.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pm.oversight_active", label="Management oversight (recruitment/treatment/FU)",
                unit_description="per active month", qty_basis="per_active_month", unit_cost=5659.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pm.oversight_close", label="Management oversight (close-out)",
                unit_description="per close-out month", qty_basis="per_closeout_month", unit_cost=6435.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pm.vendor_mgmt", label="Vendor management",
                unit_description="per month", qty_basis="per_month_capped_21", unit_cost=960.0,
                source_ref="src.kshb012_bids",
                basis="Novotech bid unit rate; capped at 21 months per the bid's own "
                      "vendor-management line.",
            ),
        ),
    ),
    # -- C6: Data Management ------------------------------------------------------
    RateCardCategory(
        name="Data Management", bucket=BudgetBucket.direct, sort_order=6,
        lines=(
            RateCardLine(
                key="dm.db_build", label="EDC database design, build and UAT",
                unit_description="Per study", qty_basis="per_study", unit_cost=150000.0,
                source_ref="src.alcoa_crio_2025",
                basis="Scaled from the published 120,000 (20-site, non-oncology "
                      "benchmark) for a 40-site, multi-language multinational build "
                      "— deliberately SEPARATE from sw.edc licensing; folding the two "
                      "together is the single most common EDC-budget understatement.",
            ),
            RateCardLine(
                key="dm.edit_checks", label="Edit-check specification",
                unit_description="Per study", qty_basis="per_study", unit_cost=20646.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="dm.external_data_setup", label="External data transfer set-up",
                unit_description="per vendor", qty_basis="per_vendor", unit_cost=3096.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="dm.dmp", label="Data management plan",
                unit_description="Per study", qty_basis="per_study", unit_cost=4695.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="dm.patient_profiles", label="Patient profile set-up",
                unit_description="Per study", qty_basis="per_study", unit_cost=42598.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="dm.rtsm_setup", label="RTSM / IRT set-up",
                unit_description="Per study", qty_basis="per_study", unit_cost=37074.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="dm.review", label="Data review and query resolution",
                unit_description="per active month", qty_basis="per_active_month", unit_cost=18503.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="dm.coding", label="Medical coding (MedDRA / WHODrug)",
                unit_description="per active month", qty_basis="per_active_month", unit_cost=8599.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="dm.external_data", label="External data handling",
                unit_description="per active month", qty_basis="per_active_month", unit_cost=250.0,
                source_ref="src.kshb012_bids",
                basis="Novotech bid unit rate. Novotech's own I09 line is explicitly "
                      "'Per Month (Recruitment, Treatment, Follow-Up)' - the active "
                      "phase, not the full study duration - so this uses the same "
                      "per_active_month basis as its sibling lines dm.review/dm.coding "
                      "rather than per_month (which was over-counting startup and "
                      "close-out months this line was never priced to cover).",
            ),
            RateCardLine(
                key="dm.rtsm_run", label="RTSM / IRT hosting and maintenance",
                unit_description="per active month", qty_basis="per_active_month", unit_cost=2178.0,
                source_ref="src.kshb012_bids",
                basis="Novotech bid unit rate. Same per_active_month correction as "
                      "dm.external_data - Novotech's I10 line is explicitly 'Per "
                      "Month (Ex Startup & Closeout)'.",
            ),
            RateCardLine(
                key="dm.rtsm_close", label="RTSM close-out",
                unit_description="Per study", qty_basis="per_study", unit_cost=3748.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="dm.site_support", label="EDC site user support",
                unit_description="per site", qty_basis="per_site", unit_cost=1155.0,
                source_ref="src.alcoa_crio_2025",
                basis="Published benchmark: 23,100 / 20 sites = 1,155/site.",
            ),
            RateCardLine(
                key="dm.dbl", label="Database lock",
                unit_description="Per study", qty_basis="per_study", unit_cost=6984.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="dm.dm_close", label="Data management close-out",
                unit_description="per close-out month", qty_basis="per_closeout_month", unit_cost=1200.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
        ),
    ),
    # -- C7: Biostatistics and Programming ----------------------------------------
    RateCardCategory(
        name="Biostatistics and Programming", bucket=BudgetBucket.direct, sort_order=7,
        lines=(
            RateCardLine(
                key="bio.sap", label="SAP and TFL shells",
                unit_description="Per study", qty_basis="per_study", unit_cost=18092.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="bio.randomization", label="Randomisation schedule generation",
                unit_description="Per study", qty_basis="per_study", unit_cost=4600.0,
                source_ref="src.internal_estimate",
                basis="No public figure found; rate-card approximation.",
            ),
            RateCardLine(
                key="bio.tlf_final", label="TLF programming (final analysis)",
                unit_description="Per study", qty_basis="per_study", unit_cost=94240.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="bio.final_analysis", label="Final statistical analysis",
                unit_description="Per study", qty_basis="per_study", unit_cost=15400.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="bio.cdisc", label="CDISC SDTM/ADaM conversion and define.xml",
                unit_description="Per study", qty_basis="per_study", unit_cost=111600.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="bio.pk_nca", label="PK non-compartmental analysis",
                unit_description="Per study", qty_basis="per_study", unit_cost=11200.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="bio.mgmt_startup", label="Biostatistics management (start-up)",
                unit_description="per start-up month", qty_basis="per_startup_month", unit_cost=1960.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="bio.mgmt_active", label="Biostatistics management (recruit/treat/FU)",
                unit_description="per active month", qty_basis="per_active_month", unit_cost=840.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="bio.mgmt_close", label="Biostatistics management (close-out)",
                unit_description="per close-out month", qty_basis="per_closeout_month", unit_cost=3640.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
        ),
    ),
    # -- C8: Pharmacovigilance and Safety ------------------------------------------
    RateCardCategory(
        name="Pharmacovigilance and Safety", bucket=BudgetBucket.direct, sort_order=8,
        lines=(
            RateCardLine(
                key="pv.db_setup", label="Safety database set-up",
                unit_description="Per study", qty_basis="per_study", unit_cost=27616.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pv.smp", label="Safety management plan",
                unit_description="Per study", qty_basis="per_study", unit_cost=1507.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pv.mgmt", label="Safety management and periodic reporting",
                unit_description="per month", qty_basis="per_safety_month", unit_cost=1801.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pv.sae", label="SAE processing, follow-up and reconciliation",
                unit_description="per SAE", qty_basis="per_sae", unit_cost=742.0,
                source_ref="src.kshb012_bids",
                basis="Novotech bid unit rate — the healthy-volunteer card assumes "
                      "ZERO SAEs; this per-event volume pricing is the core "
                      "structural difference for a patient study.",
            ),
            RateCardLine(
                key="pv.susar", label="SUSAR expedited reporting",
                unit_description="per SUSAR", qty_basis="per_susar", unit_cost=1464.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pv.dsur", label="DSUR generation",
                unit_description="per year", qty_basis="per_year", unit_cost=11966.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
        ),
    ),
    # -- C9: Medical Monitoring — IN SCOPE for patients (out-of-scope on the
    # healthy card) -----------------------------------------------------------
    RateCardCategory(
        name="Medical Monitoring", bucket=BudgetBucket.direct, sort_order=9,
        lines=(
            RateCardLine(
                key="mm.startup", label="Medical monitoring oversight (start-up)",
                unit_description="per start-up month", qty_basis="per_startup_month", unit_cost=22301.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="mm.recruitment", label="Medical monitoring oversight (recruitment)",
                unit_description="per recruitment month", qty_basis="per_recruitment_month", unit_cost=50219.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="mm.treatment", label="Medical monitoring oversight (treatment)",
                unit_description="per treatment month", qty_basis="per_treatment_month", unit_cost=31846.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="mm.closeout", label="Medical monitoring oversight (close-out)",
                unit_description="per close-out month", qty_basis="per_closeout_month", unit_cost=11432.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
        ),
    ),
    # -- C10: Clinical Study Report -------------------------------------------
    RateCardCategory(
        name="Clinical Study Report", bucket=BudgetBucket.direct, sort_order=10,
        lines=(
            RateCardLine(
                key="csr.write", label="CSR writing, QC and finalisation",
                unit_description="Per CSR", qty_basis="per_study", unit_cost=74167.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
        ),
    ),
    # -- C11: Quality Assurance -------------------------------------------------
    RateCardCategory(
        name="Quality Assurance", bucket=BudgetBucket.direct, sort_order=11,
        lines=(
            RateCardLine(
                key="qa.vendor_qual", label="Vendor qualification and QA programme",
                unit_description="Per study", qty_basis="per_study", unit_cost=23112.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="qa.site_audits", label="Site GCP audits",
                unit_description="per audit", qty_basis="per_audit", unit_cost=8500.0,
                source_ref="src.internal_estimate",
                basis="No public figure found; rate-card approximation, scaled up "
                      "from the healthy-card's single-clinic audit rate for a "
                      "multi-country programme.",
            ),
            RateCardLine(
                key="qa.tmf_audit", label="TMF audit",
                unit_description="Per study", qty_basis="per_study", unit_cost=9500.0,
                source_ref="src.internal_estimate",
                basis="No public figure found; rate-card approximation.",
            ),
        ),
    ),
    # -- C12: Translations --------------------------------------------------------
    RateCardCategory(
        name="Translations", bucket=BudgetBucket.direct, sort_order=12,
        lines=(
            RateCardLine(
                key="tr.management", label="Translation project management",
                unit_description="Per study", qty_basis="per_study", unit_cost=30618.0,
                source_ref="src.kshb012_bids",
                basis="Novotech/PPD bid unit rate for translation PROJECT "
                      "MANAGEMENT; the vendor's own per-word/per-language cost is "
                      "booked separately as pt.translations (pass-through).",
            ),
        ),
    ),
    # -- C13: Software Licensing and Hosting -----------------------------------
    RateCardCategory(
        name="Software Licensing and Hosting", bucket=BudgetBucket.software, sort_order=13,
        lines=(
            RateCardLine(
                key="sw.edc", label="EDC licensing and hosting",
                unit_description="per site-month", qty_basis="per_site_licence_month", unit_cost=500.0,
                source_ref="src.alcoa_crio_2025",
                basis="Independent 2025 benchmark works out to 411/site-month; a "
                      "real 2026 CRO bid for this exact study works out to "
                      "502/site-month — 22% apart, the expected premium for a "
                      "top-tier multinational platform. 500/site-month is the "
                      "default so the tool reprices correctly for any site count "
                      "and duration rather than hard-coding a flat monthly figure.",
            ),
            RateCardLine(
                key="sw.edc_prof", label="EDC professional services",
                unit_description="per month", qty_basis="per_edc_month", unit_cost=2461.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="sw.edc_coder", label="EDC URL / coder configuration",
                unit_description="Per study", qty_basis="per_study", unit_cost=5910.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="sw.ctms", label="CTMS licensing",
                unit_description="per year", qty_basis="per_year", unit_cost=55000.0,
                source_ref="src.kshb012_bids",
                basis="Adopted as an annualised figure from the bid's multi-year "
                      "CTMS total (168,360 over the study), so the tool reprices "
                      "correctly for a shorter or longer study. Weaker corroboration "
                      "than the EDC line: the only comparators are an algorithmic "
                      "Veeva CTMS estimate (60,000-120,000/yr, low confidence) and a "
                      "published mid-market list price (~34,550/yr).",
            ),
            RateCardLine(
                key="sw.etmf", label="eTMF licensing",
                unit_description="per year", qty_basis="per_year", unit_cost=67000.0,
                source_ref="src.kshb012_bids",
                basis="Adopted as an annualised figure from the bid's multi-year "
                      "eTMF total (202,400 over the study). Weaker corroboration: "
                      "comparators are a published mid-market list (~21,110/yr) and "
                      "Veeva's whole-product-line median contract value "
                      "(188,460/yr, not eTMF-specific).",
            ),
            RateCardLine(
                key="sw.rbqm", label="RBQM software licensing",
                unit_description="Per study", qty_basis="per_study", unit_cost=134000.0,
                source_ref="src.kshb012_bids", basis="PPD/Emmes bid unit rate.",
            ),
            RateCardLine(
                key="sw.hosting", label="Clinical systems hosting",
                unit_description="per month", qty_basis="per_month", unit_cost=2400.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="sw.safety_db", label="Safety database licensing",
                unit_description="Per study", qty_basis="per_study", unit_cost=10890.0,
                source_ref="src.kshb012_bids", basis="PPD/Emmes bid unit rate.",
            ),
            RateCardLine(
                key="sw.reporting", label="Analytics / reporting licensing",
                unit_description="Per study", qty_basis="per_study", unit_cost=11500.0,
                source_ref="src.kshb012_bids", basis="PPD/Emmes bid unit rate.",
            ),
            RateCardLine(
                key="sw.trial_desk", label="Study portal / trial desk",
                unit_description="Per study", qty_basis="per_study", unit_cost=4600.0,
                source_ref="src.kshb012_bids", basis="PPD/Emmes bid unit rate.",
            ),
            RateCardLine(
                key="sw.ai_authoring", label="AI authoring agent (CSR / SAP)",
                unit_description="Per study", qty_basis="per_study", unit_cost=9200.0,
                source_ref="src.kshb012_bids", basis="PPD/Emmes bid unit rate.",
            ),
        ),
    ),
    # -- C14: Investigator Grants and Site Fees — ALL lines geo-scaled -----------
    RateCardCategory(
        name="Investigator Grants and Site Fees", bucket=BudgetBucket.investigator_grants,
        sort_order=14,
        lines=(
            RateCardLine(
                key="ig.per_patient", label="Investigator grant per enrolled patient",
                unit_description="per patient", qty_basis="per_enrolled", unit_cost=48600.0,
                source_ref="src.kshb012_bids", geo_scaled=True,
                basis="Novotech's real bid for this exact study pays 21,882.81 per "
                      "enrolled patient (PG02, 128 patients = 2,801,000) - a directly "
                      "comparable, itemized real rate that supersedes the prior "
                      "derived US-academic-centre triangulation. That figure already "
                      "reflects Novotech's own international site mix, so it is "
                      "treated as the target SCALED value under this card's default "
                      "0.45 geography factor: base = 21,882.81 / 0.45 = 48,628, "
                      "rounded to 48,600. On a global_emerging_mixed study this line "
                      "resolves back to ~21,880/patient, matching the real bid.",
            ),
            RateCardLine(
                key="ig.screen_failure", label="Investigator grant per screen-failure patient",
                unit_description="per screen failure", qty_basis="per_screen_failure", unit_cost=3950.0,
                source_ref="src.kshb012_bids", geo_scaled=True,
                basis="Novotech's real bid (PG01) pays 1,778.57 per screen-fail "
                      "patient. Same geography back-solve as ig.per_patient: "
                      "1,778.57 / 0.45 = 3,952, rounded to 3,950.",
            ),
            RateCardLine(
                key="ig.site_startup", label="Site start-up and activation fee",
                unit_description="per site", qty_basis="per_site", unit_cost=9800.0,
                source_ref="src.kshb012_bids", geo_scaled=True,
                basis="Novotech's real bid (PG03, 'Site Fees - Set Up') pays "
                      "4,411.76 per site - directly comparable, itemized, and "
                      "superseding the prior Emory/Utah/KUMC US-academic-centre "
                      "triangulation, which reflects a materially different site "
                      "type (major NCI-designated cancer centres) than the mixed "
                      "community/regional sites a real multi-country CRO bid like "
                      "this one actually uses. Geography back-solve: 4,411.76 / "
                      "0.45 = 9,804, rounded to 9,800. The Emory/Utah/KUMC figures "
                      "remain useful context for a US-only, academic-centre-heavy "
                      "study (geography_factor 1.00 there would need its own "
                      "override, not addressed by this fix), but are no longer "
                      "this line's primary source.",
            ),
            RateCardLine(
                key="ig.irb_year1", label="IRB/EC fee, year 1 (central + local context review)",
                unit_description="per site", qty_basis="per_site", unit_cost=5600.0,
                source_ref="src.wcg_irb_2026", geo_scaled=True,
                basis="Central-IRB site-level review (~2,000-2,600) PLUS the local "
                      "institutional context review central-IRB studies still incur "
                      "(KUMC 1,750, Utah 5,100) — sponsors routinely forget the "
                      "second half.",
            ),
            RateCardLine(
                key="ig.irb_continuing", label="IRB/EC continuing review",
                unit_description="per site-year", qty_basis="per_site_year_after_first",
                unit_cost=2400.0, source_ref="src.wcg_irb_2026", geo_scaled=True,
                basis="Central-IRB continuing review (1,646-2,194/PI/year) plus "
                      "local renewal (Emory 3,375, Utah 1,360-2,670).",
            ),
            RateCardLine(
                key="ig.site_annual", label="Site annual administration and maintenance",
                unit_description="per site-year", qty_basis="per_site_year_after_first",
                unit_cost=3650.0, source_ref="src.kshb012_bids", geo_scaled=True,
                basis="Novotech's real bid (PG04, 'Site Fees - Annual Administration') "
                      "pays 1,644.44 per site-year - a directly comparable, itemized "
                      "real rate, geography back-solved the same way as ig.per_patient: "
                      "1,644.44 / 0.45 = 3,654, rounded to 3,650. Supersedes the prior "
                      "KUMC/Emory/Utah academic-centre triangulation for the same "
                      "reason given on ig.site_startup.",
            ),
            RateCardLine(
                key="ig.amendment_site", label="Protocol amendment handling (site + IRB)",
                unit_description="per site-amendment", qty_basis="per_site_amendment",
                unit_cost=1100.0, source_ref="src.wcg_irb_2026", geo_scaled=True,
                basis="WCG central-IRB amendment fee (790-1,056) plus local "
                      "amendment review (Emory 1,350, Utah 2,040-3,350). Budgeted as "
                      "near-certain by default (protocol amendments are common in "
                      "oncology trials) rather than as a contingency; no specific, "
                      "independently verifiable amendment-rate statistic backs the "
                      "\"near-certain\" framing, so treat this as an operational "
                      "judgement call, not a cited fact.",
            ),
            RateCardLine(
                key="ig.imaging_read", label="Site imaging response assessment (RECIST)",
                unit_description="per assessment", qty_basis="per_imaging_timepoint",
                unit_cost=540.0, source_ref="src.emory_2025", geo_scaled=True,
                include_when="bicr_required",
                basis="Emory 2025's published industry rate, assessed at each "
                      "imaging timepoint per response criterion.",
            ),
            RateCardLine(
                key="ig.sae_reporting", label="Site SAE / UPIRSO reporting fee",
                unit_description="per SAE", qty_basis="per_sae", unit_cost=550.0,
                source_ref="src.emory_2025", geo_scaled=True,
                basis="Between Emory's 475/event and KUMC's 650/event.",
            ),
            RateCardLine(
                key="ig.monitoring_host", label="Site monitoring visit hosting fee",
                unit_description="per monitoring day", qty_basis="per_onsite_monitoring_day",
                unit_cost=1100.0, source_ref="src.utah_fy2027", geo_scaled=True,
                basis="Midpoint of three real institutional rate cards: Utah "
                      "918/day, LSU 1,000/day, Lurie Children's 1,350/day. A "
                      "genuine gap in most budgeting tools — this is the SITE "
                      "charging to host a monitor, distinct from mon.imv_onsite "
                      "(the CRO's own CRA fee).",
            ),
            RateCardLine(
                key="ig.pharmacy_setup", label="Site pharmacy (IDS) set-up",
                unit_description="per site", qty_basis="per_site", unit_cost=3750.0,
                source_ref="src.morehouse_fy2025", geo_scaled=True,
                basis="Midpoint of Morehouse's 3,000 (simple) / 4,500 (complex) IDS "
                      "set-up fee.",
            ),
            RateCardLine(
                key="ig.pharmacy_maint", label="Site pharmacy (IDS) maintenance",
                unit_description="per site-month", qty_basis="per_site_ip_month",
                unit_cost=210.0, source_ref="src.hopkins_fy2024", geo_scaled=True,
                basis="Between Johns Hopkins' up-to-170/month drug storage fee and "
                      "Morehouse's 250/month pharmacy study-maintenance fee.",
            ),
            RateCardLine(
                key="ig.closeout", label="Site close-out fee",
                unit_description="per site", qty_basis="per_site", unit_cost=8900.0,
                source_ref="src.kshb012_bids", geo_scaled=True,
                basis="Novotech's real bid (PG05, 'Close-out and Archiving Fee') pays "
                      "4,000 per site - notably HIGHER, once geography-unscaled, than "
                      "the prior US-academic-centre anchor (Emory 4,050 was being used "
                      "directly as an unscaled figure, effectively under-crediting "
                      "close-out/archiving work once the 0.45 geography factor was "
                      "applied on top of it). Geography back-solve: 4,000 / 0.45 = "
                      "8,889, rounded to 8,900, so this line resolves back to "
                      "~4,000/site on a global_emerging_mixed study, matching the "
                      "real bid exactly instead of under-shooting it.",
            ),
            RateCardLine(
                key="ig.archiving", label="Site ISF archiving and record retention",
                unit_description="per site-year", qty_basis="per_site_retention_year",
                unit_cost=675.0, source_ref="src.emory_2025", geo_scaled=True,
                basis="THE highest-conviction correction in this card: Emory 2025 "
                      "and Utah FY2027 (the two most current schedules found) both "
                      "price archiving at exactly 675 PER YEAR, and Utah invoices 15 "
                      "years of annual fees at close-out when the contract is silent "
                      "(hence archival_retention_years default 15). A tool carrying "
                      "a single ~600 one-time archiving line understates this line "
                      "by roughly 15x.",
            ),
        ),
    ),
    # -- C15: Central Laboratory ---------------------------------------------
    RateCardCategory(
        name="Central Laboratory", bucket=BudgetBucket.pass_through, sort_order=15,
        lines=(
            RateCardLine(
                key="lab.setup", label="Central lab set-up, manual and normal ranges",
                unit_description="Per study", qty_basis="per_study", unit_cost=130000.0,
                source_ref="src.erg_aspe_2014",
                basis="Fixed-plus-variable modelling (not pure per-patient): the two "
                      "real CRO quotes examined show sample-logistics coordination "
                      "behaving as a study-level fixed cost (500/patient at n=20 vs "
                      "213/patient at n=350) — a pure per-patient model would badly "
                      "under-price a small trial.",
            ),
            RateCardLine(
                key="lab.analysis", label="Central lab analysis (safety, PK, biomarker)",
                unit_description="per patient", qty_basis="per_enrolled", unit_cost=2300.0,
                source_ref="src.erg_aspe_2014",
                basis="ERG/ASPE central-lab benchmark inflated to 2026 and adjusted "
                      "for the fixed-cost split above; should land central lab at "
                      "4-12% of the grand total per ERG/ASPE Table 2 — a sanity "
                      "check the engine surfaces as a warning.",
            ),
            RateCardLine(
                key="lab.kits", label="Laboratory kits",
                unit_description="per patient", qty_basis="per_enrolled", unit_cost=700.0,
                source_ref="src.internal_estimate",
                basis="NO public lab-kit unit price exists anywhere (checked "
                      "Labcorp Central Labs, LabConnect, Clinigen, Versiti, ACM "
                      "Global, QPS, Almac, Accurant, University of Michigan Central "
                      "Biorepository — all quote-on-request). This is a rate-card "
                      "approximation and must be labelled as such, never as "
                      "researched.",
            ),
            RateCardLine(
                key="lab.shipping", label="Sample shipping and courier",
                unit_description="per patient", qty_basis="per_enrolled", unit_cost=700.0,
                source_ref="src.neoteryx_shipping",
                basis="Built from real per-shipment frozen-sample courier rates "
                      "(domestic ~240, international ~650 in 2026 dollars) applied "
                      "across the study's site-month shipment cadence.",
            ),
            RateCardLine(
                key="lab.site_handling", label="Site-side sample processing and handling",
                unit_description="per patient", qty_basis="per_enrolled", unit_cost=500.0,
                source_ref="src.georgia_ctsa",
                basis="Built from published academic specimen-processing unit "
                      "rates (serum/plasma 19/unit, PBMC 42/unit, lab usage fee "
                      "21/visit) applied across the patient's visit schedule.",
            ),
        ),
    ),
    # -- C16: Imaging and Central Read — emitted only when bicr_required ---------
    RateCardCategory(
        name="Imaging and Central Read", bucket=BudgetBucket.pass_through, sort_order=16,
        out_of_scope_line="Imaging and Central Read (Out of Scope — no RECIST-based endpoint)",
        lines=(
            RateCardLine(
                key="img.charter", label="Imaging charter, reader training, platform set-up",
                unit_description="Per study", qty_basis="per_study", unit_cost=75000.0,
                source_ref="src.internal_estimate", include_when="bicr_required",
                basis="No public figure found for the study-level BICR charter/"
                      "platform build; rate-card approximation.",
            ),
            RateCardLine(
                key="img.site_setup", label="Site imaging activation and phantom qualification",
                unit_description="per site", qty_basis="per_site", unit_cost=6500.0,
                source_ref="src.iowa_radcore", include_when="bicr_required", geo_scaled=True,
                basis="Built from published academic radiology core rates: start-up "
                      "2,142 + annual fee (x2 years) 1,836 + phantom scan 2,500 "
                      "= ~6,478, rounded.",
            ),
            RateCardLine(
                key="img.bicr", label="Blinded independent central review",
                unit_description="per timepoint", qty_basis="per_imaging_timepoint",
                unit_cost=700.0, source_ref="src.median_2024", include_when="bicr_required",
                basis="Derived from the only real vendor-side BICR money figure "
                      "found (Median Technologies' EUR 3.1m for two Phase III "
                      "oncology imaging studies) divided across a plausible patient "
                      "and timepoint count. NEVER price a BICR read at commodity "
                      "teleradiology rates (40/CT, 99/PET-CT) — those are "
                      "radiologist labour only, roughly an order of magnitude below "
                      "a blinded, adjudicated, Part-11 BICR read.",
            ),
            RateCardLine(
                key="img.ct", label="CT tumour assessment (chest/abdomen/pelvis)",
                unit_description="per timepoint", qty_basis="per_imaging_timepoint",
                unit_cost=1200.0, status=BudgetLineStatus.not_planned,
                source_ref="src.careroute_ct74177", include_when="bicr_required",
                basis="Defaults to not_planned: the scan itself is normally inside "
                      "the per-patient investigator grant (ig.per_patient). Switch "
                      "to Costed only if imaging is billed to the sponsor "
                      "separately from the site grant.",
            ),
        ),
    ),
    # -- C17: Clinical Supply, Depot and Logistics ------------------------------
    RateCardCategory(
        name="Clinical Supply, Depot and Logistics", bucket=BudgetBucket.pass_through, sort_order=17,
        lines=(
            RateCardLine(
                key="sup.packaging", label="IMP packaging, labelling and blinding",
                unit_description="per patient", qty_basis="per_enrolled", unit_cost=650.0,
                source_ref="src.sofpromed_quote",
                basis="Built from a real vendor quotation's derived unit rates "
                      "(4.00/bottle labelling, scaled to a multi-drug-product "
                      "blinded kit).",
            ),
            RateCardLine(
                key="sup.depot_inbound", label="Depot inbound receipt",
                unit_description="per shipment", qty_basis="per_depot_inbound", unit_cost=500.0,
                source_ref="src.sofpromed_quote", basis="Real vendor quotation unit rate.",
            ),
            RateCardLine(
                key="sup.depot_storage", label="Depot storage (2-8 C)",
                unit_description="per pallet-month", qty_basis="per_pallet_month", unit_cost=100.0,
                source_ref="src.warehousing_2026",
                basis="Midpoint of the published 58-135/pallet/month refrigerated "
                      "band. WARNING: -20C and -80C storage are priced differently "
                      "(-20C 85-185/pallet/month; -80C 240-520 per RACK-equivalent, "
                      "NOT per pallet — applying a pallet basis at -80C understates "
                      "by ~10x and is the commonest error on this line).",
            ),
            RateCardLine(
                key="sup.depot_pm", label="Depot project management",
                unit_description="per month", qty_basis="per_month", unit_cost=2000.0,
                source_ref="src.sofpromed_quote",
                basis="Scaled from the real vendor quotation's ~694/month (6-site "
                      "study) to a 40-site multinational programme.",
            ),
            RateCardLine(
                key="sup.distribution", label="Depot-to-site IMP distribution",
                unit_description="per shipment", qty_basis="per_ip_shipment", unit_cost=650.0,
                source_ref="src.sofpromed_quote",
                basis="Convergence of three independent sources: the real vendor "
                      "quotation (350/shipment ambient domestic), a 2026 courier "
                      "price ladder (260-780 standard pharma courier), and a 2012 "
                      "international frozen-shipment benchmark inflated to ~680.",
            ),
            RateCardLine(
                key="sup.ip_retention", label="Long-term IP retention storage",
                unit_description="per year", qty_basis="per_ip_retention_year", unit_cost=12000.0,
                source_ref="src.internal_estimate",
                basis="No current, oncology-specific public figure found for IMP "
                      "long-term retention storage (the only regulatory figure, an "
                      "FDA BA/BE reserve-sample estimate, carries no base year and "
                      "is not used here); rate-card approximation.",
            ),
        ),
    ),
    # -- C18: Comparator Drug Sourcing — gated on comparator_sourced_by_sponsor --
    RateCardCategory(
        name="Comparator Drug Sourcing", bucket=BudgetBucket.pass_through, sort_order=18,
        lines=(
            RateCardLine(
                key="comp.drug", label="Comparator reference product acquisition",
                unit_description="per patient on comparator", qty_basis="per_comparator_patient",
                unit_cost=None, status=BudgetLineStatus.actuals_only,
                source_ref="src.bms_opdivo_list",
                basis="US list price ($33.758/mg, Jan-2026) x 6,240 mg/patient-year "
                      "course x 0.80 exposure factor x 1.30 overage factor. "
                      "Deliberately OFF by default (Budget.comparator_sourced_by_"
                      "sponsor) — for a nivolumab biosimilar this is a "
                      "potentially $10M+ line that must be a deliberate opt-in, "
                      "never a silent default. Not in COMPARATOR_PRICE_KB for any "
                      "other drug -> stays actuals_only with no invented figure.",
            ),
            RateCardLine(
                key="comp.agent_fee", label="Comparator sourcing agent service fee",
                unit_description="% of acquisition value", qty_basis="per_study",
                unit_cost=None, status=BudgetLineStatus.actuals_only,
                source_ref="src.internal_estimate",
                basis="12% of the comp.drug line's total acquisition value — "
                      "EXPLICITLY UNVERIFIED. Eight distinct search phrasings across "
                      "Patheon/Thermo Fisher, Sharp Services, Inceptua, RxSource, "
                      "Myonex, World Courier, Cencora, Clinigen, McKesson, N-SIDE "
                      "and GEP found not one published sourcing-agent markup "
                      "percentage anywhere; it is treated industry-wide as "
                      "commercially confidential. This is a placeholder, not a "
                      "researched figure.",
            ),
            RateCardLine(
                key="comp.qp_release", label="Import, QP release, over-labelling and blinding",
                unit_description="per patient on comparator", qty_basis="per_comparator_patient",
                unit_cost=None, status=BudgetLineStatus.not_planned,
                source_ref="src.internal_estimate",
                basis="No public per-unit or per-trial figure exists for QP "
                      "release, import handling, over-labelling or blinding of "
                      "sourced comparator product. Emitted not_planned with NO "
                      "number at all, rather than an invented placeholder — this "
                      "is a known, real, unbudgeted cost gap the tool surfaces "
                      "honestly instead of guessing.",
            ),
        ),
    ),
    # -- C19: Site Ancillary Equipment and Supplies ------------------------------
    RateCardCategory(
        name="Site Ancillary Equipment and Supplies", bucket=BudgetBucket.pass_through, sort_order=19,
        lines=(
            RateCardLine(
                key="anc.equipment",
                label="Site ancillary equipment (ULT freezer, refrigerated centrifuge, infusion pumps)",
                unit_description="per site", qty_basis="per_site", unit_cost=26000.0,
                source_ref="src.usalab_transcat",
                basis="Rental (1,767/site-month: freezer 1,000 + centrifuge 542 + "
                      "infusion pump 225, all src.kwipped_rentals) below an ~18-month "
                      "equipment-on-site window; flat 26,000/site purchase "
                      "(ULT freezer ~12,000 + refrigerated centrifuge ~15,000, "
                      "src.usalab_transcat) at or above it — the crossover implied "
                      "by the two verified rate sets. See unit_cost_for's "
                      "anc.equipment branch for the exact switch.",
            ),
            RateCardLine(
                key="anc.calibration", label="Annual equipment calibration and inspection",
                unit_description="per site-year", qty_basis="per_site_year", unit_cost=737.0,
                source_ref="src.hopkins_fy2024",
                basis="Johns Hopkins' published Clinical Engineering annual "
                      "maintenance fee for sponsor-provided equipment — a line most "
                      "ancillary-equipment budgets omit entirely.",
            ),
            RateCardLine(
                key="anc.monitoring", label="Continuous site temperature monitoring",
                unit_description="per site-month", qty_basis="per_site_licence_month", unit_cost=250.0,
                source_ref="src.withnell_sensors",
                basis="Scaled from the published ~19/logger/week rate for 2-4 "
                      "monitored locations per site (pharmacy fridge, -20C, -80C, "
                      "ambient room).",
            ),
            RateCardLine(
                key="anc.loggers", label="Single-use shipment temperature loggers",
                unit_description="per shipment", qty_basis="per_ip_shipment", unit_cost=90.0,
                source_ref="src.testo_logger",
                basis="One per shipment, driven off shipment count rather than "
                      "site count.",
            ),
        ),
    ),
    # -- C20: Pass-Through and Site Costs -----------------------------------------
    RateCardCategory(
        name="Pass-Through and Site Costs", bucket=BudgetBucket.pass_through, sort_order=20,
        lines=(
            RateCardLine(
                key="pt.ha_fees", label="Competent authority submission fees",
                unit_description="Per study", qty_basis="per_study", unit_cost=45270.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pt.irb_fees", label="Central IRB/EC review fees",
                unit_description="per country", qty_basis="per_country", unit_cost=7500.0,
                source_ref="src.kshb012_bids", basis="Bid-calibrated unit rate.",
            ),
            RateCardLine(
                key="pt.legal_rep", label="Legal representation",
                unit_description="per month", qty_basis="per_month", unit_cost=1370.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
            RateCardLine(
                key="pt.translations", label="Translation vendor costs",
                unit_description="Per study", qty_basis="per_study", unit_cost=120000.0,
                source_ref="src.kshb012_bids", basis="PPD/Emmes bid unit rate.",
            ),
            RateCardLine(
                key="pt.monitoring_travel", label="Monitoring travel and expenses",
                unit_description="per monitoring trip", qty_basis="per_monitoring_trip", unit_cost=1100.0,
                source_ref="src.alcoa_crio_2025",
                basis="Published 1,000/visit North-American travel benchmark, "
                      "rounded up slightly for a multinational trip mix.",
            ),
            RateCardLine(
                key="pt.investigator_meeting", label="Investigator meeting planning and logistics",
                unit_description="Per study", qty_basis="per_study", unit_cost=57050.0,
                source_ref="src.kshb012_bids", basis="PPD/Emmes bid unit rate.",
            ),
            RateCardLine(
                key="pt.site_binders", label="Site binders / ISF supplies",
                unit_description="per site", qty_basis="per_site", unit_cost=833.0,
                source_ref="src.kshb012_bids", basis="Bid-calibrated unit rate.",
            ),
            RateCardLine(
                key="pt.tmf_archival", label="TMF archival (sponsor side)",
                unit_description="Per study", qty_basis="per_study", unit_cost=15000.0,
                source_ref="src.internal_estimate",
                basis="THE SINGLE WEAKEST FIGURE IN THIS CARD: no eTMF/GxP-archival "
                      "vendor (Arkivum, Docbyte, SureClinical, Flex Databases, ZAS, "
                      "Ardington Archives) publishes pricing. This is a derivation "
                      "from raw records-centre box-storage rates "
                      "(~400 boxes x 6.90/box/year x 5 years), NOT a researched "
                      "vendor price, and 5 years is BELOW the EU CTR's 25-year TMF "
                      "requirement — the tool should challenge that assumption "
                      "rather than silently accept it.",
            ),
            RateCardLine(
                key="pt.admin", label="Administrative fixed expense",
                unit_description="per month", qty_basis="per_month", unit_cost=1700.0,
                source_ref="src.kshb012_bids", basis="Novotech bid unit rate.",
            ),
        ),
    ),
    # -- C21: Subject Costs — both not_planned by default; folded into the ---
    # investigator grant in oncology, unlike the healthy card's stipend model --
    RateCardCategory(
        name="Subject Costs", bucket=BudgetBucket.stipend, sort_order=21,
        lines=(
            RateCardLine(
                key="pat.travel", label="Patient travel and out-of-pocket reimbursement",
                unit_description="per patient visit", qty_basis="per_patient_visit",
                unit_cost=120.0, status=BudgetLineStatus.not_planned,
                source_ref="src.internal_estimate",
                basis="Defaults not_planned: in oncology this sits inside the "
                      "investigator grant. Emitted with a priced unit rate visible "
                      "rather than omitted, so switching it on does not silently "
                      "double-count against ig.per_patient.",
            ),
            RateCardLine(
                key="pat.caregiver", label="Caregiver / carer stipend",
                unit_description="per patient visit", qty_basis="per_caregiver_visit",
                unit_cost=75.0, status=BudgetLineStatus.not_planned,
                source_ref="src.internal_estimate",
                basis="Defaults not_planned, same rationale as pat.travel. There is "
                      "no healthy-volunteer-style completion stipend for patients — "
                      "that category does not exist on this card.",
            ),
        ),
    ),
    # -- C22: CRO/Vendor Variable Costs — all actuals_only, priced but not summed -
    RateCardCategory(
        name="CRO/Vendor Variable Costs", bucket=BudgetBucket.variable, sort_order=22,
        lines=(
            RateCardLine(
                key="var.extra_imv", label="Additional interim monitoring visit",
                unit_description="per visit", qty_basis="rate_card_only", unit_cost=5500.0,
                status=BudgetLineStatus.actuals_only, source_ref="src.alcoa_crio_2025",
                basis="Published on-site IMV all-in rate (4,500 fee + 1,000 "
                      "travel); priced but not yet incurred.",
            ),
            RateCardLine(
                key="var.extra_amendment", label="Additional protocol amendment (full CRO cost)",
                unit_description="per amendment", qty_basis="rate_card_only", unit_cost=23594.0,
                status=BudgetLineStatus.actuals_only, source_ref="src.kshb012_bids",
                basis="Bid-calibrated; priced but not yet incurred.",
            ),
            RateCardLine(
                key="var.extra_sae", label="Additional SAE above plan",
                unit_description="per SAE", qty_basis="rate_card_only", unit_cost=742.0,
                status=BudgetLineStatus.actuals_only, source_ref="src.kshb012_bids",
                basis="Bid-calibrated; priced but not yet incurred.",
            ),
            RateCardLine(
                key="var.extra_susar", label="Additional SUSAR above plan",
                unit_description="per SUSAR", qty_basis="rate_card_only", unit_cost=1464.0,
                status=BudgetLineStatus.actuals_only, source_ref="src.kshb012_bids",
                basis="Bid-calibrated; priced but not yet incurred.",
            ),
            RateCardLine(
                key="var.ind_safety_report", label="IND safety report processing (site)",
                unit_description="per report", qty_basis="rate_card_only", unit_cost=50.0,
                status=BudgetLineStatus.actuals_only, source_ref="src.emory_2025",
                basis="Emory's published per-report fee; priced but not yet "
                      "incurred.",
            ),
            RateCardLine(
                key="var.reconsent", label="Subject re-consent",
                unit_description="per subject per amendment", qty_basis="rate_card_only",
                unit_cost=270.0, status=BudgetLineStatus.actuals_only, source_ref="src.emory_2025",
                basis="Emory's published per-participant reconsent fee; priced but "
                      "not yet incurred.",
            ),
            RateCardLine(
                key="var.new_site", label="Additional site activation",
                unit_description="per site", qty_basis="rate_card_only", unit_cost=26000.0,
                status=BudgetLineStatus.actuals_only, source_ref="src.emory_2025",
                basis="Same published figure as ig.site_startup; priced but not "
                      "yet incurred.",
            ),
            RateCardLine(
                key="var.new_country", label="Additional country start-up",
                unit_description="per country", qty_basis="rate_card_only", unit_cost=6471.0,
                status=BudgetLineStatus.actuals_only, source_ref="src.kshb012_bids",
                basis="Bid-calibrated; priced but not yet incurred.",
            ),
        ),
    ),
)


def patient_default_categories() -> list[tuple[str, BudgetBucket]]:
    """The patient-card analogue of default_categories() — the 22-row seed
    template for a budget whose subject_profile is 'patient'."""
    return [(c.name, c.bucket) for c in PATIENT_RATE_CARD]


# ---------------------------------------------------------------------------
# L. Self-consistency guard for the patient card — mirrors section I above.
# Also validates every source_ref actually resolves, so a typo'd citation
# fails loudly at import time instead of silently losing its provenance.
# ---------------------------------------------------------------------------

assert len(PATIENT_RATE_CARD) == 22, (
    f"expected 22 patient rate-card categories, found {len(PATIENT_RATE_CARD)}"
)
_all_patient_keys = [l.key for c in PATIENT_RATE_CARD for l in c.lines]
assert len(set(_all_patient_keys)) == len(_all_patient_keys), (
    "duplicate PATIENT_RATE_CARD line key detected"
)
del _all_patient_keys

for _c in PATIENT_RATE_CARD:
    for _l in _c.lines:
        assert _l.source_ref is None or _l.source_ref in SOURCE_REGISTRY, (
            f"{_l.key} references unknown source {_l.source_ref!r}"
        )
        assert _l.source_ref is None or SOURCE_REGISTRY[_l.source_ref].tier in PROVENANCE_TIERS, (
            f"{_l.key} source {_l.source_ref!r} has an unrecognized provenance tier"
        )
del _c, _l
