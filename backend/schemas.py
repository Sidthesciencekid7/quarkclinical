"""Pydantic request/response schemas for all API routers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, computed_field

from models import LiteratureSource, PKParameter, ProtocolStatus, StudyType, UserRole
from services.design_decisions import classify_drug_modality


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


class DrugCreate(BaseModel):
    inn: str
    brand_names: list[str] = []
    rxnorm_id: Optional[str] = None


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


class ProtocolCreate(BaseModel):
    drug_id: uuid.UUID
    study_type: StudyType
    # population_config may include optional generation hints:
    # dose_strength, formulation, analytical_method, lloq,
    # acceptance_criteria, irb_info, org_name
    # (within-subject CV is NOT accepted here — it is always sourced from the
    # literature so the powering value keeps a citable provenance.)
    population_config: dict[str, Any] = {}
    guideline_version: str = "FDA-BE-2014"
    # The finalized study-design decisions from the "Review design" step
    # (population, dose, route, design, PD co-primary), each {key,label,value,...}.
    # Empty = the backend derives them fresh. See services/design_decisions.py.
    design_decisions: list[dict[str, Any]] = []
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
# Three-stage pipeline (Haiku classify -> Opus generate -> Sonnet validate)
# ---------------------------------------------------------------------------


class PipelineGenerateRequest(BaseModel):
    """Free-form drug name / query plus optional structured overrides.

    ``user_params`` may carry ``drug_inn``, ``t_half_hours``, ``tmax_hours``,
    ``is_nti`` — any present value overrides what Stage 1 (Haiku) extracts before
    the deterministic facts are computed for Stage 2 (Opus).

    The within-subject CV is NOT accepted as an override: it is always the
    AI/literature-retrieved value so the powering figure keeps a traceable
    provenance.
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
