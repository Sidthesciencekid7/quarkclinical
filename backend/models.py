import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

# The guideline version shipped with this platform build.
# When FDA publishes a new BE guidance, bump this constant.
# All protocols store their locked version at creation time; comparing against
# this constant drives the guideline_outdated flag in API responses.
CURRENT_GUIDELINE_VERSION = "FDA-BE-2014"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PKParameter(str, enum.Enum):
    cmax = "Cmax"
    auc0t = "AUC0t"
    auc0inf = "AUC0inf"
    tmax = "Tmax"
    t_half = "t_half"
    vd = "Vd"
    cl = "CL"
    bioavailability = "bioavailability"
    protein_binding = "protein_binding"


class LiteratureSource(str, enum.Enum):
    pubmed = "pubmed"
    dailymed = "dailymed"
    ct_gov = "ct_gov"


class StudyType(str, enum.Enum):
    be = "BE"
    biosimilar_pk = "biosimilar_pk"
    food_effect = "food_effect"


class ProtocolStatus(str, enum.Enum):
    draft = "draft"
    review = "review"
    approved = "approved"


class UserRole(str, enum.Enum):
    generator = "generator"
    reviewer = "reviewer"
    approver = "approver"


class LicenseStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"
    # Self-serve org creation lands here, not "active" — sales-assisted,
    # no Stripe: the operator manually flips it via
    # POST /api/orgs/{id}/activate-license once a deal is confirmed
    # out-of-band. See routers/orgs.py.
    pending = "pending"


class LeadStatus(str, enum.Enum):
    new = "new"
    contacted = "contacted"
    won = "won"
    lost = "lost"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    # Manages this org's team/license (join link, seat roster) — a separate axis
    # from `role`, which encodes GxP content-authorship separation of duties
    # (generator/reviewer/approver), not org administration.
    is_org_owner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    protocols: Mapped[list["Protocol"]] = relationship("Protocol", back_populates="creator")


class Organization(Base):
    """A real org entity, mapped 1:1 to the pre-existing free-text `org_id`
    strings on User/Protocol via `slug` — deliberately additive so the
    existing org-isolation checks (which compare those string columns
    directly) are never touched by this. `join_token` is the org's single
    shareable team-invite link/code; regenerating it just overwrites the
    column (see routers/orgs.py)."""
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    join_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    licenses: Mapped[list["License"]] = relationship("License", back_populates="organization")


class License(Base):
    """Seat capacity + status for an org. No payment integration yet — see the
    enterprise-licensing plan; `status`/`seat_count` are settable directly so
    the team-provisioning flow (org creation, invites, seat limits) is fully
    real and usable before billing is wired in."""
    __tablename__ = "licenses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # native_enum=False: a plain VARCHAR + CHECK constraint rather than a
    # Postgres CREATE TYPE ... ENUM — avoids native-enum migration friction
    # (e.g. ALTER TYPE ADD VALUE's own transactional quirks) for a status
    # column that's likely to gain values (trialing, past_due, ...) once
    # payment is wired in.
    status: Mapped[LicenseStatus] = mapped_column(
        Enum(LicenseStatus, name="license_status", native_enum=False, length=20),
        nullable=False, default=LicenseStatus.active,
    )
    seat_count: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    organization: Mapped["Organization"] = relationship("Organization", back_populates="licenses")


class Lead(Base):
    """A 'Contact sales' submission from the landing page's Enterprise plan.
    No CRM/notification service exists — the operator checks GET /api/leads
    (admin-gated) and follows up by hand; ``status`` is updated manually as
    the deal progresses."""
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    team_size_estimate: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus, name="lead_status", native_enum=False, length=20),
        nullable=False, default=LeadStatus.new,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Comment(Base):
    """A reviewer note attached to one section of one protocol.

    Persisted server-side (not localStorage) so notes are shared across the
    whole organization and attributed to a real user — a review trail that
    survives browsers and hand-offs. Org isolation is enforced at the router
    via get_protocol_scoped on the parent protocol; the comment itself carries
    no org_id of its own.
    """
    __tablename__ = "comments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    protocol_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("protocols.id", ondelete="CASCADE"), nullable=False, index=True
    )
    section_name: Mapped[str] = mapped_column(String(128), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable: dev/no-auth mode can post without a resolvable users-table row.
    author_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_name: Mapped[str] = mapped_column(String(255), nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Drug(Base):
    __tablename__ = "drugs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    inn: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    brand_names: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    rxnorm_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    pk_parameters: Mapped[list["PKParameterRecord"]] = relationship("PKParameterRecord", back_populates="drug")
    literature: Mapped[list["LiteratureCache"]] = relationship("LiteratureCache", back_populates="drug")
    protocols: Mapped[list["Protocol"]] = relationship("Protocol", back_populates="drug")


class PKParameterRecord(Base):
    __tablename__ = "pk_parameters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    drug_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drugs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parameter: Mapped[PKParameter] = mapped_column(Enum(PKParameter, name="pk_parameter"), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    population: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_pmid: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    drug: Mapped["Drug"] = relationship("Drug", back_populates="pk_parameters")


class LiteratureCache(Base):
    __tablename__ = "literature_cache"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pmid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    drug_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drugs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[LiteratureSource] = mapped_column(Enum(LiteratureSource, name="literature_source"), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ttl_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    drug: Mapped["Drug"] = relationship("Drug", back_populates="literature")


class Protocol(Base):
    __tablename__ = "protocols"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    drug_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("drugs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    study_type: Mapped[StudyType] = mapped_column(Enum(StudyType, name="study_type"), nullable=False)
    population_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sections: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[ProtocolStatus] = mapped_column(
        Enum(ProtocolStatus, name="protocol_status"), nullable=False, default=ProtocolStatus.draft
    )
    guideline_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # Async-generation lifecycle. Real AI generation runs for minutes, too long
    # to hold a single HTTP request open behind the edge proxy (it drops the
    # connection -> the browser sees "Failed to fetch"). So create_protocol
    # returns a "generating" row immediately and the pipeline fills it in a
    # background task; the client polls GET /protocols/{id} until this flips.
    # server_default "complete" so every pre-existing row, and every non-
    # generation path (clone, section edits), is treated as done.
    generation_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="complete"
    )
    # Human-readable reason when generation_status == "failed" (credit
    # exhaustion, a low-confidence halt, an unexpected error). Surfaced to the
    # user by the polling UI in place of the old inline HTTP error.
    generation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    drug: Mapped["Drug"] = relationship("Drug", back_populates="protocols")
    creator: Mapped["User"] = relationship("User", back_populates="protocols")
    versions: Mapped[list["ProtocolVersion"]] = relationship(
        "ProtocolVersion",
        back_populates="protocol",
        order_by="ProtocolVersion.version",
    )

    @property
    def guideline_outdated(self) -> bool:
        # Registry-aware: a protocol is "outdated" only if its locked guideline
        # is superseded/unknown — NOT merely because it differs from the FDA
        # default. EMA and ICH M13 protocols are current in their own right.
        from services.guidelines import is_current  # lazy: avoid import cycle
        return not is_current(self.guideline_version)


class ProtocolVersion(Base):
    """Immutable snapshot of a protocol's sections at a specific version number.

    Created whenever sections are modified (AI regen, manual edit, or signing).
    Combined with audit_logs, this provides a complete 21 CFR Part 11 audit trail.
    """
    __tablename__ = "protocol_versions"
    __table_args__ = (
        UniqueConstraint("protocol_id", "version", name="uq_protocol_versions_protocol_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    protocol_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("protocols.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    sections: Mapped[dict] = mapped_column(JSONB, nullable=False)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # AI_REGEN | MANUAL_EDIT | INITIAL | SIGNED
    change_type: Mapped[str] = mapped_column(String(64), nullable=False)
    change_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    protocol: Mapped["Protocol"] = relationship("Protocol", back_populates="versions")


class AuditLog(Base):
    """Append-only audit trail — no update or delete endpoints permitted."""
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    diff: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class StudyReport(Base):
    """A Clinical Study Report (CSR) built from a completed protocol's REAL results.

    The post-study bookend to Protocol. The company uploads observed study data;
    the platform computes the PK (NCA) and the bioequivalence verdict
    DETERMINISTICALLY into `results` (never AI-touched), and the AI drafts the
    ICH E3 narrative into `sections` around those locked numbers.

    Raw subject-level clinical data is intentionally NOT persisted (results-only
    retention): only `dataset_checksum` + `dataset_summary` remain as provenance
    of what was analysed. Reuses the async generation lifecycle
    (generation_status/error) introduced for protocols in migration 007.
    """
    __tablename__ = "study_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    protocol_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("protocols.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Denormalised from the parent protocol so the shared org-isolation checks
    # (which compare this string column directly) apply unchanged.
    org_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 'nca' (raw concentration-time uploaded → NCA run here) or 'params'
    # (subject-level PK parameters uploaded → NCA skipped, straight to BE stats).
    input_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="params")
    # SHA-256 of the uploaded results file + a non-identifying summary (counts,
    # treatments, periods). The raw rows themselves are deliberately discarded.
    dataset_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dataset_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # The LOCKED computed numbers: per-subject NCA, BE analysis (GMR/CI/CV/
    # verdict), safety summary. Assembled deterministically — the LLM never
    # writes or alters anything in here.
    results: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # AI-drafted ICH E3 narrative sections (prose framing the locked numbers).
    sections: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Async generation lifecycle, identical semantics to Protocol (migration 007).
    generation_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="complete"
    )
    generation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    protocol: Mapped["Protocol"] = relationship("Protocol")
    versions: Mapped[list["StudyReportVersion"]] = relationship(
        "StudyReportVersion",
        back_populates="study_report",
        order_by="StudyReportVersion.version",
    )


class StudyReportVersion(Base):
    """Immutable snapshot of a CSR's sections + results at a version number.

    Combined with audit_logs this gives the CSR the same 21 CFR Part 11 trail as
    Protocol/ProtocolVersion.
    """
    __tablename__ = "study_report_versions"
    __table_args__ = (
        UniqueConstraint("study_report_id", "version", name="uq_study_report_versions_sr_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    study_report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("study_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    sections: Mapped[dict] = mapped_column(JSONB, nullable=False)
    results: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # INITIAL | ANALYSIS | AI_REGEN | MANUAL_EDIT | SIGNED
    change_type: Mapped[str] = mapped_column(String(64), nullable=False)
    change_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    study_report: Mapped["StudyReport"] = relationship("StudyReport", back_populates="versions")
