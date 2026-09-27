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


class BudgetBucket(str, enum.Enum):
    """Which rollup line a category feeds. The rollup math keys on THIS, never
    on the category's name — so a renamed or user-added category still totals
    correctly.

    investigator_grants and software (added for the patient/oncology rate
    card, services/budget_rate_card.PATIENT_RATE_CARD) are ALWAYS 0.00 for a
    healthy-volunteer budget — no healthy-card category ever uses them — so
    adding them changes no existing total. They are kept separate from
    `pass_through` and `direct` deliberately: all three real oncology CRO
    bids this card is calibrated against (Novotech, PPD/Emmes, IQVIA) present
    investigator grants/site fees and software/hosting licensing as their own
    line items, outside professional fees and outside vendor pass-through,
    and the CRO discount applies to professional fees only — never to grants
    or software licences. Two new buckets keep that arithmetic honest with no
    special-casing in the rollup.
    """
    direct = "direct"
    stipend = "stipend"
    variable = "variable"
    pass_through = "pass_through"
    investigator_grants = "investigator_grants"
    software = "software"


class BudgetLineStatus(str, enum.Enum):
    """Why a line does or does not carry a dollar figure.

    Only `costed` rows are summed. The other five are the real, observed CRO
    vocabulary for "this row exists but has no total": they must be first-class
    states, not a null total with no explanation.
      costed         — a real cost to be priced/summed. Every costed row is
                       created fully priced by the rate-card auto-population
                       engine (services/budget_engine.py); the user may edit
                       it afterwards.
      out_of_scope   — "Out of Scope for <CRO>"
      not_applicable — "Not Applicable"
      not_planned    — "Not Planned"
      outsourced     — "Outsourced to Vendor"
      actuals_only   — "Actuals incurred" (rate-card only; the whole
                       CRO/Vendor Variable and Pass-Through categories)
    """
    costed = "costed"
    out_of_scope = "out_of_scope"
    not_applicable = "not_applicable"
    not_planned = "not_planned"
    outsourced = "outsourced"
    actuals_only = "actuals_only"


class BudgetLineSource(str, enum.Enum):
    """Where this row's numbers came from.
      manual           — the user typed this row (or moved/renamed one)
      protocol_derived — the QUANTITY came from the linked protocol's real
                         computed sampling/visit schedule. Its unit cost is
                         still a rate-card approximation; the row's note and
                         Budget.derivation say so.
      rate_card        — both quantity and unit cost are approximations from
                         services/budget_rate_card.py, calibrated against real
                         signed CRO budgets. Editable, and traceable via
                         Budget.derivation["assumptions"] / ["lines"].

    "The user has since edited it by hand" is deliberately NOT a fourth value:
    `source` is a provenance badge that must survive an edit (see
    tests/test_budgets.py::test_patch_cannot_change_source_or_derivation_key).
    Edited-ness is instead answered by comparing a row's current unit_cost to
    Budget.derivation["auto_priced"][derivation_key].
    """
    manual = "manual"
    protocol_derived = "protocol_derived"
    rate_card = "rate_card"
    researched = "researched"

    # researched — the UNIT COST is a real, cited, externally-published figure
    #     (an institutional fee schedule, a manufacturer list price, a
    #     peer-reviewed benchmark, a government rate) or a documented
    #     triangulation of such figures, not an approximation. The full
    #     citation, its provenance tier and any caveat live in
    #     Budget.derivation["sources"][source_id]; the line's own derivation
    #     record carries source_id. Like every other value here this is a
    #     provenance badge that SURVIVES a user edit — once the user retypes
    #     the price, "edited-ness" is still answered by comparing unit_cost to
    #     Budget.derivation["auto_priced"][derivation_key], never by this
    #     field. See services/budget_rate_card.RateCardSource /
    #     PROVENANCE_TIERS / SOURCE_REGISTRY for the patient rate card's
    #     citations. Precedence when a row could be more than one thing:
    #     protocol_derived (quantity from the linked protocol) wins over
    #     researched (a cited unit cost) wins over rate_card (uncited
    #     approximation).


class BudgetDiscountType(str, enum.Enum):
    """`none` exists because the rollup formula has an explicit else-branch
    (discount_amount = 0). Modelling 'no discount' as a state rather than as a
    percent of 0 keeps the UI honest about whether a discount was negotiated."""
    none = "none"
    percent = "percent"
    flat = "flat"


class BudgetStatus(str, enum.Enum):
    """Lifecycle of the budget document itself. (This is the ONE enum beyond the
    four the brief listed — it is additive, not an alternative to anything
    specified, and mirrors ProtocolStatus/LicenseStatus house practice.)"""
    draft = "draft"
    final = "final"
    archived = "archived"


class BudgetSubjectProfile(str, enum.Enum):
    """Which RATE CARD prices this budget — orthogonal to `study_type`.

    `study_type` (BE / food_effect / biosimilar_pk) keeps selecting the
    *scientific* shape of the study. This selects the *commercial* shape:

      healthy — the existing, frozen 17-category healthy-volunteer card
                (services/budget_rate_card.HEALTHY_RATE_CARD, alias
                RATE_CARD). Single clinic, CRO-run, stipend-paying, no
                investigator grants, no SAEs assumed.
      patient — the new 22-category patient card
                (services/budget_rate_card.PATIENT_RATE_CARD). Multi-site,
                multi-country, phase-priced, investigator-grant-dominated.

    A biosimilar_pk study can be either healthy or patient (e.g. an
    oncology biosimilar cannot ethically run in healthy volunteers) — the
    two axes are independent and both are recorded on every budget.

    Defaults to `healthy` on every pre-existing row (see migration 010's
    backfill) so no historic budget is ever silently re-classified.
    """
    healthy = "healthy"
    patient = "patient"


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


class Budget(Base):
    """A clinical study budget — the second tool in the QuarkClinical suite.

    Standalone by design: `protocol_id` is OPTIONAL — a drug name alone is
    enough for services/budget_engine.py to produce a complete, fully-priced
    budget from services/budget_rate_card.py. When a protocol IS linked, the
    engine derives REAL cost-driver quantities from that protocol's
    already-computed visit/sampling schedule and prices them the same way.
    Every dollar figure is an approximation calibrated against real signed CRO
    budgets, never left blank — and `derivation` records exactly which
    assumption produced which figure, so every number stays traceable and
    editable.

    ondelete="SET NULL" on protocol_id (deliberately NOT CASCADE): a budget is a
    commercial document with its own life; deleting the source protocol must
    orphan the link, not destroy the budget.
    """
    __tablename__ = "budgets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Plain indexed string holding the org SLUG — NEVER an FK to organizations.id.
    # Compared directly as budget.org_id == actor.org_id, exactly like Protocol/StudyReport.
    org_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    protocol_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("protocols.id", ondelete="SET NULL"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sponsor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cro_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default="USD")
    status: Mapped[BudgetStatus] = mapped_column(
        Enum(BudgetStatus, name="budget_status", native_enum=False, length=20),
        nullable=False, default=BudgetStatus.draft,
    )

    # -- Study-level assumptions (the header block of every real CRO budget) --
    # drug_inn / study_type / n_enrolled are AUTO-FILLED from a linked protocol
    # (and remain user-editable afterwards). The rest are ALWAYS user-entered:
    # there is no site-count, screen-failure, alternate-subject, dosing-group or
    # project-duration concept anywhere in the protocol engine, so deriving them
    # would be invention. n_screened / n_screen_failures are stored EXACTLY as
    # typed and are never back-computed from screen_failure_rate_pct.
    drug_inn: Mapped[str | None] = mapped_column(String(255), nullable=True)
    study_type: Mapped[str | None] = mapped_column(String(32), nullable=True)   # display string, e.g. "BE"
    n_enrolled: Mapped[int | None] = mapped_column(Integer, nullable=True)
    screen_failure_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_screen_failures: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_alternates: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_screened: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_sites: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_dosing_groups: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # For a healthy-volunteer budget this IS the whole study duration, as
    # before. For a patient budget it is DERIVED as the sum of the five phase
    # durations below (services/budget_engine resolves and writes it back);
    # editing it directly on a patient budget scales the five phases
    # proportionally rather than being treated as an independent input.
    project_duration_months: Mapped[float | None] = mapped_column(Float, nullable=True)

    # -- Patient/oncology rate-card profile (all NULLABLE; every column here is
    # untouched — NULL — on every existing healthy-volunteer budget, and the
    # 010 migration backfills subject_profile='healthy' on every pre-existing
    # row so no historic budget can ever be silently re-classified as a
    # patient budget). See services/budget_rate_card.PATIENT_RATE_CARD and
    # PATIENT_STUDY_PROFILES for how these drive the patient card's pricing.
    subject_profile: Mapped[BudgetSubjectProfile | None] = mapped_column(
        Enum(BudgetSubjectProfile, name="budget_subject_profile", native_enum=False, length=16),
        nullable=True,
    )
    therapeutic_area: Mapped[str | None] = mapped_column(String(64), nullable=True)   # "oncology" | "other" | "unknown"
    indication: Mapped[str | None] = mapped_column(String(255), nullable=True)
    n_countries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Sites CONTRACTED (n_sites, above, is sites ACTIVATED — the two differ in
    # a real multi-country patient study; they are the same number for the
    # single-clinic healthy-volunteer card).
    n_sites_selected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dropout_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_completed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase-based durations (patient studies are priced per phase-month, not
    # against one flat duration): start-up, recruitment, treatment, follow-up,
    # close-out. Their sum is project_duration_months, above.
    startup_months: Mapped[float | None] = mapped_column(Float, nullable=True)
    recruitment_months: Mapped[float | None] = mapped_column(Float, nullable=True)
    treatment_months: Mapped[float | None] = mapped_column(Float, nullable=True)
    followup_months: Mapped[float | None] = mapped_column(Float, nullable=True)
    closeout_months: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_expected_saes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_expected_susars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_monitoring_visits_onsite: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_monitoring_visits_remote: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_pharmacy_visits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_visits_per_patient: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_protocol_amendments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # e.g. "us_only" | "eu_west" | "eu_east" | "latam" | "apac" | "china" |
    # "india" | "global_emerging_mixed" | "global_developed_mixed" — see
    # services/budget_rate_card.GEOGRAPHY_FACTORS.
    geography_profile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Derived from geography_profile by default; a direct user edit overrides
    # the profile lookup (recorded with derivation source "user").
    geography_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    archival_retention_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bicr_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Comparator drug acquisition (e.g. reference-product nivolumab for a
    # biosimilar PK-similarity arm) defaults OFF: it is a potentially
    # multi-million-dollar line that must be a deliberate opt-in, never a
    # silent default — see services/budget_rate_card.COMPARATOR_PRICE_KB.
    comparator_sourced_by_sponsor: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False,
    )
    comparator_arm_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)

    # -- Discount (applies to the DIRECT bucket subtotal only) ---------------
    discount_type: Mapped[BudgetDiscountType] = mapped_column(
        Enum(BudgetDiscountType, name="budget_discount_type", native_enum=False, length=20),
        nullable=False, default=BudgetDiscountType.none,
    )
    discount_value: Mapped[float] = mapped_column(Float, nullable=False, server_default="0", default=0.0)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provenance of the last protocol-derivation run: which protocol, when, the
    # exact counts used (n_enrolled, PK timepoints, ADA timepoints, visit days)
    # and any honesty warnings. Mirrors the platform-wide "show your work"
    # principle used by _sample_size / _sampling_schedule.
    derivation: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    protocol: Mapped["Protocol | None"] = relationship("Protocol")
    categories: Mapped[list["BudgetCategory"]] = relationship(
        "BudgetCategory",
        back_populates="budget",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="BudgetCategory.sort_order",
    )


class BudgetCategory(Base):
    """One cost section of a budget (Recruiting, Clinical Conduct, ...).

    Seeded from budget_engine.DEFAULT_CATEGORIES — a starting TEMPLATE, not a
    hardcoded enum: every row is renameable, re-bucketable, reorderable and
    deletable. `bucket` is what the rollup math keys on, not the name.

    No org_id of its own: isolation is enforced through the parent Budget via
    get_budget_scoped, exactly like Comment does through Protocol.
    """
    __tablename__ = "budget_categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    budget_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budgets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    bucket: Mapped[BudgetBucket] = mapped_column(
        # length=32 (widened from 20 by migration 010): "investigator_grants"
        # is 19 characters, one short of the old VARCHAR(20) limit.
        Enum(BudgetBucket, name="budget_bucket", native_enum=False, length=32),
        nullable=False, default=BudgetBucket.direct,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    budget: Mapped["Budget"] = relationship("Budget", back_populates="categories")
    line_items: Mapped[list["BudgetLineItem"]] = relationship(
        "BudgetLineItem",
        back_populates="category",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="BudgetLineItem.sort_order",
    )


class BudgetLineItem(Base):
    """One priced (or explicitly un-priced) row of a budget category.

    Every money field is NULLABLE, but only because some rows carry NO dollar
    figure BY DESIGN — only a status: "Out of Scope", "Not Applicable", "Not
    Planned", "Outsourced to Vendor", "Actuals incurred" (the CRO-Variable and
    Pass-Through categories are ENTIRELY rate-card, with no knowable total
    until incurred). Every `costed` row, by contrast, is created fully priced
    by services/budget_engine.py's rate-card auto-population — quantity AND
    unit_cost AND total are all filled in automatically, from a linked
    protocol's real schedule when there is one and from calibrated per-study
    defaults when there is not. `total` is INDEPENDENTLY SETTABLE and is never
    force-recomputed by the server afterwards: many real rows are lump sums
    with no visible quantity x unit_cost arithmetic.

    `source` distinguishes a row whose QUANTITY the system found in the linked
    protocol's real computed schedule ('protocol_derived') from one priced
    purely off the rate card ('rate_card') from one the user typed from
    scratch ('manual') — see BudgetLineSource. Editedness is NOT part of
    `source`: compare a row's current unit_cost against
    Budget.derivation["auto_priced"][derivation_key] instead.
    """
    __tablename__ = "budget_line_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("budget_categories.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    label: Mapped[str] = mapped_column(String(512), nullable=False)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Free text exactly as seen in real budgets: "Per study", "per screened
    # subject", "per subject", "per week", "per month", "per year", "per dose",
    # "per amendment", "per visit", "per draw".
    unit_description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[BudgetLineStatus] = mapped_column(
        Enum(BudgetLineStatus, name="budget_line_status", native_enum=False, length=20),
        nullable=False, default=BudgetLineStatus.costed,
    )
    source: Mapped[BudgetLineSource] = mapped_column(
        # length=32 (widened from 20 by migration 010) for "researched".
        Enum(BudgetLineSource, name="budget_line_source", native_enum=False, length=32),
        nullable=False, default=BudgetLineSource.manual,
    )
    # Stable key identifying WHICH derivation produced this row
    # ("pk_draws" | "ada_draws" | "subject_visits"); NULL for manual rows.
    # Re-derivation matches on this so a user-entered price is never clobbered.
    derivation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Free-text rationale AND revision history — real budgets use this column
    # literally ("Changed as subject number increased from 172 to 176",
    # "Removed the cost", "Added additional lab data transmissions").
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    category: Mapped["BudgetCategory"] = relationship("BudgetCategory", back_populates="line_items")
