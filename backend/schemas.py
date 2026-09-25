"""Pydantic request/response schemas for all API routers."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from models import (
    BudgetBucket,
    BudgetDiscountType,
    BudgetLineSource,
    BudgetLineStatus,
    BudgetStatus,
    BudgetSubjectProfile,
    LiteratureSource,
    PKParameter,
    ProtocolStatus,
    StudyType,
    UserRole,
)
from services.design_decisions import classify_drug_modality
from services.patient_population import sanitize_user_indication


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class UserRead(BaseModel):
    """Never includes hashed_password — this is the only user-facing shape."""
    id: uuid.UUID
    org_id: str
    email: str
    role: UserRole

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Organizations / licenses / team
# ---------------------------------------------------------------------------


class OrgCreate(BaseModel):
    name: str
    # Only used when Clerk auth is disabled (local dev/tests) — ignored when a
    # verified Clerk session is present, since the email then comes from that
    # session instead. See routers/orgs.py::_resolve_new_user_email.
    email: Optional[str] = None


class OrgJoinRequest(BaseModel):
    token: str
    email: Optional[str] = None


class MemberRead(BaseModel):
    id: uuid.UUID
    email: str
    role: UserRole
    is_org_owner: bool

    model_config = ConfigDict(from_attributes=True)


class LicenseRead(BaseModel):
    status: str
    seat_count: int
    seats_used: int
    period_start: datetime
    period_end: datetime


class OrgRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    is_owner: bool
    license: Optional[LicenseRead] = None
    # Only populated for the org owner — see routers/orgs.py::get_my_org.
    join_token: Optional[str] = None


class AdminOrgRead(BaseModel):
    """Operator-only view (GET /api/orgs/admin/list) — every org at a glance
    so the operator can find who to activate (or suspend) after a sales
    call."""
    id: uuid.UUID
    name: str
    slug: str
    owner_email: Optional[str] = None
    license_status: Optional[str] = None
    seat_count: Optional[int] = None
    seats_used: int
    # Days left in the current license period — only meaningful once active
    # (a pending license's period_end isn't a real deadline yet); None for
    # pending/suspended/no-license orgs. Negative would mean an active
    # license whose period has lapsed without being renewed.
    days_remaining: Optional[int] = None
    created_at: datetime


class LicenseActivateRequest(BaseModel):
    """Manual, no-Stripe license activation (POST /api/orgs/{id}/activate-license)."""
    seat_count: Optional[int] = None
    period_days: int = 365


# ---------------------------------------------------------------------------
# Leads ("Contact sales")
# ---------------------------------------------------------------------------


class LeadCreate(BaseModel):
    company_name: str
    contact_name: str
    contact_email: str
    team_size_estimate: Optional[str] = None
    message: Optional[str] = None


class LeadRead(BaseModel):
    id: uuid.UUID
    company_name: str
    contact_name: str
    contact_email: str
    team_size_estimate: Optional[str] = None
    message: Optional[str] = None
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Drug
# ---------------------------------------------------------------------------


_INN_PATTERN = re.compile(r"^[A-Za-z][A-Za-z\s-]{1,63}$")


class DrugCreate(BaseModel):
    inn: str
    brand_names: list[str] = []
    rxnorm_id: Optional[str] = None

    @field_validator("inn")
    @classmethod
    def _validate_inn(cls, v: str) -> str:
        v = v.strip()
        if not _INN_PATTERN.match(v):
            raise ValueError("inn must be 2-64 letters, spaces, or hyphens only")
        return v.lower()


class DrugRead(BaseModel):
    id: uuid.UUID
    inn: str
    brand_names: list[str]
    rxnorm_id: Optional[str] = None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @computed_field  # type: ignore[misc]
    @property
    def modality(self) -> str:
        """"biologic" | "small_molecule" | "unknown" — drives which study types
        are selectable for this drug in the wizard (see design_decisions.py)."""
        return classify_drug_modality(self.inn)


# ---------------------------------------------------------------------------
# Literature
# ---------------------------------------------------------------------------


class LiteratureFetchRequest(BaseModel):
    drug_id: uuid.UUID
    sources: list[LiteratureSource] = [
        LiteratureSource.pubmed,
        LiteratureSource.dailymed,
    ]


class LiteratureItemRead(BaseModel):
    pmid: str
    title: Optional[str] = None
    abstract: Optional[str] = None
    source: str
    fetched_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# PK Parameters
# ---------------------------------------------------------------------------


class PKParamRead(BaseModel):
    id: uuid.UUID
    parameter: PKParameter
    value: float
    unit: str
    population: Optional[str] = None
    source_pmid: str
    confidence: Optional[float] = None
    extracted_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def _sanitize_user_indication_in_config(v: Any) -> Any:
    """Replace a client-supplied population_config['user_indication'] with its
    sanitised form, and drop the key when nothing usable remains."""
    if isinstance(v, dict) and "user_indication" in v:
        v = dict(v)
        clean = sanitize_user_indication(v.get("user_indication"))
        if clean is None:
            v.pop("user_indication", None)
        else:
            v["user_indication"] = clean
    return v


class ProtocolCreate(BaseModel):
    # population_config carries optional, all-defaulting generation hints. It is
    # a free-form dict for backwards compatibility: every key below is optional
    # and previously-stored configs keep loading unchanged.
    #
    # Document / study hints:
    #   dose_strength, formulation, analytical_method, lloq, acceptance_criteria,
    #   irb_info, org_name, dose_mg
    #
    # cv_intra (float, %) — INVESTIGATOR-SPECIFIED within-subject CV.
    #   Supplying it OVERRIDES the literature-derived value used to power the
    #   study AND CHANGES THE DECLARED PROVENANCE: the recommendation's
    #   cv_source becomes "override (investigator-specified; not
    #   literature-derived)", cv_is_user_override becomes True, and cv_table —
    #   the supporting-trials table the SAP renders as the justification for the
    #   powering figure — is emptied, because an investigator-specified number
    #   has no literature provenance to cite. The literature rows are still
    #   retained for the record under cv_literature_not_used_for_powering.
    #   Omit it and the CV is sourced from the literature (highest observed
    #   within-subject CV for Cmax or AUC across healthy-volunteer studies), or
    #   falls back to a clearly-flagged assumption when nothing is retrievable.
    #   See routers/protocols.py::_resolve_powering_cv.
    #
    # Deterministic-engine hints (each engine degrades gracefully without them):
    #   dosing ('single'|'multiple'), dosing_interval_hours (tau, hours)
    #       -> services/sampling_schedule.py
    #   additional_pk_timepoints_hours (free text, e.g. "36, 48, 72") — extra PK
    #       draws in hours post-dose, added to the auto-computed schedule (not a
    #       replacement). Numbers are extracted permissively; anything outside
    #       the computed sampling horizon is disclosed as rejected, not silently
    #       dropped. -> services/sampling_schedule.py's additional_timepoints_hours
    #   additional_immunogenicity_timepoints_months (free text, e.g. "9, 15") —
    #       extra ADA draws in months post-first-dose. Only meaningful for a
    #       multiple-dose biosimilar patient study (the only design with a
    #       comparative-period-linked ADA schedule at all).
    #       -> services/comparative_safety.py's additional_immunogenicity_months,
    #          threaded into services/sampling_schedule.py too so the two ADA
    #          schedules this app computes never disagree.
    #       NOTE: 'dosing' is now SET BY THE SERVER from the Review-design
    #       Population / Dose decisions (a multi-dose or steady-state choice, or an
    #       engine TMDD call, becomes 'multiple'), so the sample-size, sampling,
    #       comparative-safety, randomization and CRF engines all see it.
    #   user_indication (str) — the sponsor's own study indication for a PATIENT
    #       biosimilar study. Sanitised server-side (control characters and every
    #       quote/markup character removed, capped at 200 chars) and treated as
    #       DATA only: it never reaches the prompt as free text, only quoted
    #       inside the locked patient-population directive. Dropped when the final
    #       study is not a patient biosimilar_pk study.
    #       -> services/patient_population.py
    #   is_ind, is_applicable_clinical_trial, is_new_chemical_entity, radiolabeled
    #       -> services/safety_reporting.py (sponsor regulatory-affairs overrides)
    #   randomization_seed (int) -> services/randomization.py; recorded so the
    #       allocation list can be regenerated years later. Normally derived from
    #       the protocol's own UUID and written back here by the server.
    #   icf_version, icf_date, protocol_number, protocol_version, protocol_title,
    #   investigator_name, site_name, study_contact, rights_contact,
    #   injury_contact, compensation_text, confinement_hours,
    #   n_pk_draws_per_period, clinicaltrials_gov_id, prohibited_medications,
    #   safety_text -> services/icf.py (absent ones become visible placeholders)
    #
    # Server-computed keys are UNDERSCORE-PREFIXED and are never accepted from a
    # client (any client-supplied one is overwritten at creation): _sample_size,
    # _stratification, _design_decisions, _sampling_schedule, _ddi,
    # _safety_gating, _bioanalytical, _safety_reporting, _randomization,
    # _crf_dmp, _icf, _label, _comparative_safety, _patient_population,
    # _trial_precedent. They are stripped from the prompt's population_config
    # and surfaced as their own locked-fact context keys.
    drug_id: uuid.UUID
    study_type: StudyType
    population_config: dict[str, Any] = {}
    guideline_version: str = "FDA-BE-2014"
    # The finalized study-design decisions from the "Review design" step
    # (population, dose, route, design, PD co-primary), each {key,label,value,...}.
    # Empty = the backend derives them fresh. See services/design_decisions.py.
    design_decisions: list[dict[str, Any]] = []

    @field_validator("population_config")
    @classmethod
    def _sanitize_population_config(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _sanitize_user_indication_in_config(v)

    # created_by and org_id are NOT accepted here — a client-supplied creator id
    # could claim authorship of anyone (audit-trail spoofing), and a client-
    # supplied org_id would let any actor file a protocol under a org they
    # don't belong to (defeats org isolation entirely). create_protocol()
    # derives both from the authenticated actor (X-User-Id -> users table).


class DesignPreviewRequest(BaseModel):
    """Inputs for the credit-free 'Review design' preview — returns the five
    auto-determined design decisions for the drug before any AI generation."""
    drug_id: uuid.UUID
    study_type: StudyType
    population_config: dict[str, Any] = {}
    # The reviewer's explicit Population choice on the Review design step, when it
    # differs from the engine's call; Dose / PD are re-derived from it.
    population_choice: Optional[str] = Field(default=None, max_length=64)

    @field_validator("population_config")
    @classmethod
    def _sanitize_population_config(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _sanitize_user_indication_in_config(v)


class IndicationPreviewRequest(BaseModel):
    """Inputs for the credit-free indication preview: ranks the reference
    product's indications for a patient biosimilar study and, when the user typed
    their own, reports how it compares."""
    drug_id: uuid.UUID
    study_type: StudyType
    user_indication: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("user_indication")
    @classmethod
    def _sanitize_indication(cls, v: Optional[str]) -> Optional[str]:
        return sanitize_user_indication(v)


class ProtocolRead(BaseModel):
    id: uuid.UUID
    drug_id: uuid.UUID
    org_id: str
    study_type: StudyType
    population_config: dict[str, Any]
    sections: dict[str, Any]
    version: int
    status: ProtocolStatus
    guideline_version: str
    # True when the locked guideline_version is older than CURRENT_GUIDELINE_VERSION.
    # Sourced from Protocol.guideline_outdated @property — never changes for a given record.
    guideline_outdated: bool = False
    # Async-generation lifecycle: "generating" while the background pipeline runs,
    # "complete" once sections are filled, "failed" if it couldn't. The client
    # polls this after create_protocol returns. generation_error carries the
    # human-readable reason when status == "failed".
    generation_status: str = "complete"
    generation_error: str | None = None
    created_by: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProtocolSummary(BaseModel):
    """Lightweight row for the 'My Reports' list — no sections payload, but
    includes the drug's INN/brand names so the UI can render and re-open a past
    protocol without a second fetch."""
    id: uuid.UUID
    drug_id: uuid.UUID
    drug_inn: str
    drug_brand_names: list[str] = []
    study_type: StudyType
    guideline_version: str
    guideline_outdated: bool = False
    version: int
    status: ProtocolStatus
    section_count: int
    created_at: datetime


class SectionPatch(BaseModel):
    # Caller may pass extra context to override or supplement the stored context.
    context_override: dict[str, Any] = {}


class SectionSave(BaseModel):
    content: str


# ---------------------------------------------------------------------------
# Protocol version history
# ---------------------------------------------------------------------------


class ProtocolVersionSummary(BaseModel):
    """Lightweight metadata row — used in the version list endpoint."""
    id: uuid.UUID
    version: int
    change_type: str
    change_detail: Optional[dict[str, Any]] = None
    changed_by: Optional[uuid.UUID] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProtocolVersionRead(BaseModel):
    """Full snapshot — used in the single-version-fetch endpoint."""
    id: uuid.UUID
    protocol_id: uuid.UUID
    version: int
    sections: dict[str, Any]
    changed_by: Optional[uuid.UUID] = None
    change_type: str
    change_detail: Optional[dict[str, Any]] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Diff view
# ---------------------------------------------------------------------------


class SectionDiff(BaseModel):
    section_name: str
    display_name: str
    status: Literal["unchanged", "modified", "added", "removed"]
    old_content: Optional[str] = None   # raw stored value (may be HTML)
    new_content: Optional[str] = None
    diff_lines: list[str] = []          # unified-diff lines of plain text
    word_count_delta: int = 0


class VersionDiffResult(BaseModel):
    protocol_id: uuid.UUID
    from_version: int
    to_version: int
    from_created_at: Optional[datetime] = None
    to_created_at: Optional[datetime] = None
    sections_changed: int
    sections: list[SectionDiff]


# ---------------------------------------------------------------------------
# E-signature
# ---------------------------------------------------------------------------


class ESignRequest(BaseModel):
    signatory_name: str
    reason: str = "Protocol approved for regulatory submission"
    # 21 CFR Part 11 §11.200(a)(1): the signature must require re-execution of
    # an identity-verification component at the moment of signing — a prior
    # login session is not sufficient. The signer re-enters their password
    # here; it is verified against the stored hash and never persisted.
    reauth_password: str


class ESignResponse(BaseModel):
    protocol_id: uuid.UUID
    version: int
    sha256_hash: str
    signed_at: datetime
    signatory_id: uuid.UUID
    signatory_email: str
    status: ProtocolStatus
    # The fixed, role-derived meaning bound to this signature (e.g. "Approved"),
    # per 21 CFR Part 11 §11.50(a) — never client-supplied, so it can't be
    # spoofed to claim a meaning the signer's role doesn't hold.
    meaning: str


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class ExportRequest(BaseModel):
    protocol_id: uuid.UUID


# ---------------------------------------------------------------------------
# Three-stage pipeline (Sonnet classify -> Sonnet generate -> Sonnet validate)
# ---------------------------------------------------------------------------


class PipelineGenerateRequest(BaseModel):
    """Free-form drug name / query plus optional structured overrides.

    ``user_params`` may carry ``drug_inn``, ``cv_intra_pct``, ``t_half_hours``,
    ``tmax_hours``, ``is_nti`` — any present value overrides what Stage 1 (Sonnet)
    extracts before the deterministic facts are computed for Stage 2 (Sonnet).

    Supplying ``cv_intra_pct`` overrides the retrieved within-subject CV AND
    changes the declared provenance: the computed sample_size comes back with
    ``cv_source`` = "override (investigator-specified; not literature-derived)"
    and ``cv_is_user_override`` = True, so no downstream consumer can present it
    as a literature-sourced figure.
    """
    query: str
    user_params: dict[str, Any] = {}


class PipelineLogEntry(BaseModel):
    stage: str
    model: str
    status: str                     # success | halted | failed
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    error: Optional[str] = None


class PipelineGenerateResponse(BaseModel):
    classification: dict[str, Any]
    protocol: str
    validation: list[dict[str, Any]]
    pipeline_log: list[PipelineLogEntry]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditLogRead(BaseModel):
    id: uuid.UUID
    user_id: Optional[uuid.UUID] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    diff: Optional[dict[str, Any]] = None
    ip_address: Optional[str] = None
    timestamp: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Section review comments
# ---------------------------------------------------------------------------


class CommentCreate(BaseModel):
    section_name: str
    text: str


class CommentRead(BaseModel):
    id: uuid.UUID
    protocol_id: uuid.UUID
    section_name: str
    text: str
    # author_id is deliberately not exposed — the display name (email) is all
    # the review panel needs.
    author_name: str
    resolved: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CommentUpdate(BaseModel):
    resolved: bool


# ---------------------------------------------------------------------------
# Study reports (CSR)
# ---------------------------------------------------------------------------


class StudyReportCreate(BaseModel):
    """Create a CSR shell from a completed protocol. ``input_kind`` selects the
    upload contract: 'nca' (raw concentration-time → NCA run server-side) or
    'params' (subject-level PK parameters already computed → straight to BE stats)."""
    input_kind: Literal["nca", "params"] = "params"


class StudyReportRead(BaseModel):
    id: uuid.UUID
    protocol_id: uuid.UUID
    org_id: str
    input_kind: str
    dataset_checksum: Optional[str] = None
    dataset_summary: dict[str, Any] = {}
    # The locked, deterministically-computed results (NCA / BE / safety).
    results: dict[str, Any] = {}
    # AI-drafted ICH E3 narrative.
    sections: dict[str, Any] = {}
    version: int
    # Async lifecycle: 'generating' while the narrative pipeline runs, else
    # 'complete'/'failed'. The dataset analysis itself is synchronous on upload.
    generation_status: str = "complete"
    generation_error: Optional[str] = None
    created_by: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StudyReportSummary(BaseModel):
    """Lightweight row for listing a protocol's CSRs (omits the big JSON blobs).
    ``has_results``/``verdict`` are derived in the router, not from the ORM."""
    id: uuid.UUID
    protocol_id: uuid.UUID
    input_kind: str
    version: int
    generation_status: str
    has_results: bool = False
    verdict: Optional[str] = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


class BudgetLineItemCreate(BaseModel):
    label: str
    quantity: Optional[float] = None
    unit_cost: Optional[float] = None
    unit_description: Optional[str] = None
    # Independently settable: many real rows are lump sums with no unit math.
    # OMIT the field entirely to let the server compute quantity*unit_cost;
    # SEND it (even as null) and the server stores exactly what you sent.
    total: Optional[float] = None
    status: BudgetLineStatus = BudgetLineStatus.costed
    note: Optional[str] = None
    sort_order: Optional[int] = None
    # `source` / `derivation_key` are deliberately absent — only the engine mints
    # a protocol_derived row, so a client cannot fake the derived badge.


class BudgetLineItemUpdate(BaseModel):
    # Every field optional; the router applies model_dump(exclude_unset=True),
    # so omitted = untouched and explicit null = cleared.
    label: Optional[str] = None
    quantity: Optional[float] = None
    unit_cost: Optional[float] = None
    unit_description: Optional[str] = None
    total: Optional[float] = None
    status: Optional[BudgetLineStatus] = None
    note: Optional[str] = None
    sort_order: Optional[int] = None
    # Move the row to another category OF THE SAME BUDGET.
    category_id: Optional[uuid.UUID] = None


class BudgetLineItemRead(BaseModel):
    id: uuid.UUID
    category_id: uuid.UUID
    label: str
    quantity: Optional[float] = None
    unit_cost: Optional[float] = None
    unit_description: Optional[str] = None
    total: Optional[float] = None
    status: BudgetLineStatus
    source: BudgetLineSource
    derivation_key: Optional[str] = None
    note: Optional[str] = None
    sort_order: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BudgetCategoryCreate(BaseModel):
    name: str
    bucket: BudgetBucket = BudgetBucket.direct
    note: Optional[str] = None
    sort_order: Optional[int] = None


class BudgetCategoryUpdate(BaseModel):
    name: Optional[str] = None
    bucket: Optional[BudgetBucket] = None
    note: Optional[str] = None
    sort_order: Optional[int] = None


class BudgetCategoryRead(BaseModel):
    id: uuid.UUID
    budget_id: uuid.UUID
    name: str
    bucket: BudgetBucket
    sort_order: int
    note: Optional[str] = None
    line_items: list[BudgetLineItemRead] = []
    # Computed by budget_engine.category_subtotal — the router fills this in
    # (see _category_read); it is never stored.
    subtotal: float = 0.0

    model_config = ConfigDict(from_attributes=True)


class BudgetRollup(BaseModel):
    """The exact arithmetic of the reference signed budgets. Recomputed on every
    read — never persisted, so it can never drift from the line items."""
    direct_subtotal: float
    discount_type: str
    discount_value: float
    discount_amount: float
    total_direct_costs: float
    subject_stipend_total: float
    variable_total: float
    pass_through_total: float
    # NEW (spec §2.0/§7.5) — always 0.0 for a healthy-volunteer budget, since
    # no healthy-card category ever uses either bucket.
    investigator_grants_total: float = 0.0
    software_total: float = 0.0
    grand_total: float
    category_subtotals: dict[str, float] = {}


# Patient/oncology rate-card fields shared verbatim between BudgetCreate and
# BudgetUpdate (spec §7.2/§7.3) — every field optional, so omitting all of
# them preserves today's exact healthy-volunteer behaviour. Mixed into both
# via multiple inheritance rather than duplicated.
class _BudgetPatientProfileFields(BaseModel):
    # Plain str (not the enum type) so an invalid value reaches the router as
    # ordinary text and gets the exact spec-worded 422 message — the same
    # pattern `study_type` already uses — rather than pydantic's own enum
    # error text.
    subject_profile: Optional[str] = None
    therapeutic_area: Optional[str] = None
    indication: Optional[str] = None
    n_countries: Optional[int] = None
    n_sites_selected: Optional[int] = None
    dropout_rate_pct: Optional[float] = None
    startup_months: Optional[float] = None
    recruitment_months: Optional[float] = None
    treatment_months: Optional[float] = None
    followup_months: Optional[float] = None
    closeout_months: Optional[float] = None
    n_expected_saes: Optional[int] = None
    n_expected_susars: Optional[int] = None
    n_monitoring_visits_onsite: Optional[int] = None
    n_monitoring_visits_remote: Optional[int] = None
    n_pharmacy_visits: Optional[int] = None
    n_visits_per_patient: Optional[int] = None
    n_protocol_amendments: Optional[int] = None
    geography_profile: Optional[str] = None
    geography_factor: Optional[float] = None
    archival_retention_years: Optional[int] = None
    bicr_required: Optional[bool] = None
    comparator_sourced_by_sponsor: Optional[bool] = None
    comparator_arm_fraction: Optional[float] = None

    # -- 422 validation (spec §7.2) ------------------------------------------
    @field_validator("geography_factor")
    @classmethod
    def _validate_geography_factor(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.05 <= v <= 2.0):
            raise ValueError("geography_factor must be between 0.05 and 2.0.")
        return v

    @field_validator("dropout_rate_pct")
    @classmethod
    def _validate_dropout_rate_pct(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0 <= v <= 100):
            raise ValueError("dropout_rate_pct must be between 0 and 100.")
        return v

    @field_validator("comparator_arm_fraction")
    @classmethod
    def _validate_comparator_arm_fraction(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0 <= v <= 1):
            raise ValueError("comparator_arm_fraction must be between 0 and 1.")
        return v

    @field_validator(
        "startup_months", "recruitment_months", "treatment_months",
        "followup_months", "closeout_months",
    )
    @classmethod
    def _validate_phase_months_nonneg(cls, v: Optional[float], info) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError(f"{info.field_name} cannot be negative.")
        return v

    @field_validator(
        "n_countries", "n_sites_selected", "n_expected_saes", "n_expected_susars",
        "n_monitoring_visits_onsite", "n_monitoring_visits_remote", "n_pharmacy_visits",
        "n_visits_per_patient", "n_protocol_amendments", "archival_retention_years",
    )
    @classmethod
    def _validate_patient_ints_nonneg(cls, v: Optional[int], info) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError(f"{info.field_name} cannot be negative.")
        return v


class BudgetCreate(_BudgetPatientProfileFields):
    # A drug name alone is enough to produce a complete, fully-priced budget.
    # `name` is derived from drug_inn when omitted; at least one of the two
    # must be present (422 otherwise).
    name: Optional[str] = None
    drug_inn: Optional[str] = None
    # Optional hint; when a protocol is linked its study_type wins unless the
    # caller sent one explicitly. Must be a StudyType value ("BE",
    # "biosimilar_pk", "food_effect") or 422.
    study_type: Optional[str] = None
    # OPTIONAL accuracy boost, NOT an either/or with drug_inn any more — a
    # budget is fully usable with no protocol at all. When present, the
    # protocol is org-checked and its real computed schedule seeds the
    # Clinical Conduct cost drivers (and every other rate-card quantity that
    # has a protocol-side source).
    protocol_id: Optional[uuid.UUID] = None
    sponsor_name: Optional[str] = None
    cro_name: Optional[str] = None
    currency: str = "USD"
    # Study-level assumptions. Any value supplied here WINS over both the
    # protocol and the rate card's per-study-type default.
    n_enrolled: Optional[int] = None
    n_sites: Optional[int] = None
    n_dosing_groups: Optional[int] = None
    project_duration_months: Optional[float] = None
    screen_failure_rate_pct: Optional[float] = None
    n_screen_failures: Optional[int] = None
    n_alternates: Optional[int] = None
    n_screened: Optional[int] = None
    notes: Optional[str] = None


class BudgetUpdate(_BudgetPatientProfileFields):
    # NOTE: protocol_id is intentionally NOT here — linking/unlinking is a
    # side-effecting operation (it derives line items), so it gets its own
    # endpoint rather than hiding inside a generic field patch.
    name: Optional[str] = None
    sponsor_name: Optional[str] = None
    cro_name: Optional[str] = None
    currency: Optional[str] = None
    status: Optional[BudgetStatus] = None
    drug_inn: Optional[str] = None
    study_type: Optional[str] = None
    n_enrolled: Optional[int] = None
    screen_failure_rate_pct: Optional[float] = None
    n_screen_failures: Optional[int] = None
    n_alternates: Optional[int] = None
    n_screened: Optional[int] = None
    n_sites: Optional[int] = None
    n_dosing_groups: Optional[int] = None
    project_duration_months: Optional[float] = None
    discount_type: Optional[BudgetDiscountType] = None
    discount_value: Optional[float] = None
    notes: Optional[str] = None


class BudgetLinkProtocol(BaseModel):
    """null unlinks (line items are kept — they are real work already done)."""
    protocol_id: Optional[uuid.UUID] = None


class ReorderRequest(BaseModel):
    ordered_ids: list[uuid.UUID]


class BudgetRead(_BudgetPatientProfileFields):
    """Full budget document. Hand-assembled in the router (no from_attributes)
    because categories carry computed subtotals and the rollup is computed, not
    stored — same reason ProtocolSummary/StudyReportSummary are hand-assembled."""
    id: uuid.UUID
    org_id: str
    protocol_id: Optional[uuid.UUID] = None
    name: str
    sponsor_name: Optional[str] = None
    cro_name: Optional[str] = None
    currency: str
    status: BudgetStatus
    drug_inn: Optional[str] = None
    study_type: Optional[str] = None
    n_enrolled: Optional[int] = None
    screen_failure_rate_pct: Optional[float] = None
    n_screen_failures: Optional[int] = None
    n_alternates: Optional[int] = None
    n_screened: Optional[int] = None
    n_sites: Optional[int] = None
    n_dosing_groups: Optional[int] = None
    project_duration_months: Optional[float] = None
    # Server-computed for a patient budget (§3.2); None on a healthy one.
    n_completed: Optional[int] = None
    discount_type: BudgetDiscountType
    discount_value: float
    notes: Optional[str] = None
    derivation: dict[str, Any] = {}
    categories: list[BudgetCategoryRead] = []
    rollup: BudgetRollup
    created_at: datetime
    updated_at: datetime


class BudgetSummary(BaseModel):
    """Lightweight row for the budgets list tile — no line items, but the
    grand total so the list can show money without a second fetch. Hand-assembled
    in the router, no model_config (the ProtocolSummary convention)."""
    id: uuid.UUID
    name: str
    protocol_id: Optional[uuid.UUID] = None
    drug_inn: Optional[str] = None
    study_type: Optional[str] = None
    status: BudgetStatus
    currency: str
    n_enrolled: Optional[int] = None
    # Plain str (not the enum type) so an invalid value reaches the router as
    # ordinary text and gets the exact spec-worded 422 message — the same
    # pattern `study_type` already uses — rather than pydantic's own enum
    # error text.
    subject_profile: Optional[str] = None
    therapeutic_area: Optional[str] = None
    n_sites: Optional[int] = None
    n_countries: Optional[int] = None
    category_count: int
    line_item_count: int
    grand_total: float
    created_at: datetime
    updated_at: datetime


class BudgetProfilePreviewDefaults(BaseModel):
    """The `defaults` sub-object of BudgetProfilePreview (spec §7.4)."""
    n_enrolled: Optional[int] = None
    n_sites: Optional[int] = None
    n_countries: Optional[int] = None
    project_duration_months: Optional[float] = None
    startup_months: Optional[float] = None
    recruitment_months: Optional[float] = None
    treatment_months: Optional[float] = None
    followup_months: Optional[float] = None
    closeout_months: Optional[float] = None
    n_expected_saes: Optional[int] = None
    geography_profile: Optional[str] = None


class BudgetProfilePreview(BaseModel):
    """GET /api/budgets/profile-preview?drug_inn=<str> — spec §7.4. Cheap,
    read-only, no DB write, no AI call: powers the create-view hint by running
    the SAME resolution/pricing the create endpoint would, against a
    transient, never-persisted Budget."""
    drug_inn: str
    modality: str
    allowed_study_types: list[str]
    study_type: str
    subject_profile: BudgetSubjectProfile
    subject_profile_source: str
    subject_profile_basis: str
    therapeutic_area: str
    profile_key: Optional[str] = None
    defaults: BudgetProfilePreviewDefaults
    estimated_grand_total: float
    category_count: int


class BudgetRateCardMeta(BaseModel):
    """GET /api/budgets/rate-card-meta?subject_profile=<healthy|patient> —
    spec §7.4. Static metadata for building UI without hardcoding the card."""
    subject_profile: BudgetSubjectProfile
    rate_card_version: str
    currency: str
    disclaimer: str
    categories: list[dict[str, Any]]
    buckets: list[str]
    provenance_tiers: dict[str, str]
    geography_profiles: dict[str, float]
