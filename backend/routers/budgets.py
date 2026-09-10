"""Clinical study budgets — the second tool in the Quark AI suite.

A budget is standalone by design: a drug name alone is enough for
services/budget_engine.py to produce a complete, fully-priced budget from
services/budget_rate_card.py — no protocol required. When one IS linked, the
engine derives REAL cost-driver QUANTITIES from that protocol's
already-computed visit/sampling schedule and prices them the same way. Every
dollar figure is an approximation calibrated against real signed CRO budgets,
never left blank, and every figure's provenance is recorded in
Budget.derivation so it stays traceable and editable.

AUTHORIZATION: every route resolves the verified actor, then either scopes a
plain org filter (list/create) or fetches the budget through get_budget_scoped
(everything else), so a budget belonging to another org is always a 403 (and
an unknown one a 404) — the same isolation rule the protocol/CSR routes use.

ROLE GATING: none, deliberately (mirrors comments.py). The generator/reviewer/
approver split encodes GxP separation of duties over PROTOCOL CONTENT; a
budget is a commercial/operational document, not a signed regulatory artifact,
so any authenticated org member may build and price one.

GRANULARITY: fine-grained resource endpoints (category / line-item CRUD), not
a single whole-document PATCH — see the module docstring rationale recorded in
the architecture spec: house style (comments.py, protocols.py section patches),
a real per-row audit trail, safe multi-editor concurrency, and payload size.
The two /reorder endpoints are the one deliberate bulk exception.

Router is NOT nested under /api/protocols/{protocol_id} (unlike comments.py):
a budget must be creatable and listable with no protocol at all. protocol_id
is instead an optional body field on create, and a dedicated link-protocol
endpoint handles linking/re-linking/unlinking (a side-effecting operation that
derives line items, so it does not hide inside a generic field patch).
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth import require_user
from database import get_db
from dependencies import (
    audit_logger,
    authenticate_actor as _authenticate_actor,
    get_budget_scoped,
    get_protocol_scoped,
)
from models import (
    AuditLog,
    Budget,
    BudgetCategory,
    BudgetDiscountType,
    BudgetLineItem,
    BudgetLineSource,
    BudgetSubjectProfile,
    StudyType,
)
from routers.export import _content_disposition, _DOCX_MEDIA_TYPE
from schemas import (
    BudgetCategoryCreate,
    BudgetCategoryRead,
    BudgetCategoryUpdate,
    BudgetCreate,
    BudgetLineItemCreate,
    BudgetLineItemRead,
    BudgetLineItemUpdate,
    BudgetLinkProtocol,
    BudgetProfilePreview,
    BudgetProfilePreviewDefaults,
    BudgetRateCardMeta,
    BudgetRead,
    BudgetRollup,
    BudgetSummary,
    BudgetUpdate,
    ReorderRequest,
)
from services import budget_docx_export, budget_engine, budget_rate_card
from services.design_decisions import allowed_study_types, classify_drug_modality

# The full set of new patient/oncology fields (spec §1.3/§7.2/§7.3) — shared
# between create and update so both stay in lock-step with the schema.
_PATIENT_PROFILE_FIELDS = (
    "subject_profile", "therapeutic_area", "indication", "n_countries",
    "n_sites_selected", "dropout_rate_pct", "startup_months", "recruitment_months",
    "treatment_months", "followup_months", "closeout_months", "n_expected_saes",
    "n_expected_susars", "n_monitoring_visits_onsite", "n_monitoring_visits_remote",
    "n_pharmacy_visits", "n_visits_per_patient", "n_protocol_amendments",
    "geography_profile", "geography_factor", "archival_retention_years",
    "bicr_required", "comparator_sourced_by_sponsor", "comparator_arm_fraction",
)

_SUBJECT_PROFILE_VALUES = {p.value for p in BudgetSubjectProfile}


def _validate_subject_profile(value: str | None) -> None:
    if value is not None and value not in _SUBJECT_PROFILE_VALUES:
        raise HTTPException(
            422,
            detail=f"Unknown subject_profile '{value}'. Expected one of: "
                   + ", ".join(sorted(_SUBJECT_PROFILE_VALUES)),
        )


def _validate_geography_profile(value: str | None) -> None:
    if value is not None and value not in budget_rate_card.GEOGRAPHY_FACTORS:
        raise HTTPException(
            422,
            detail=f"Unknown geography_profile '{value}'. Expected one of: "
                   + ", ".join(sorted(budget_rate_card.GEOGRAPHY_FACTORS)),
        )

router = APIRouter(
    prefix="/api/budgets",
    tags=["budgets"],
    dependencies=[Depends(audit_logger)],
)


# ---------------------------------------------------------------------------
# Assembly helpers
# ---------------------------------------------------------------------------


def _category_read(cat: BudgetCategory) -> BudgetCategoryRead:
    out = BudgetCategoryRead.model_validate(cat)
    out.subtotal = budget_engine.category_subtotal(cat)
    return out


def _budget_read(budget: Budget) -> BudgetRead:
    cats = sorted(budget.categories, key=lambda c: c.sort_order)
    rollup = budget_engine.compute_rollups(
        budget.categories,
        discount_type=budget.discount_type,
        discount_value=budget.discount_value,
    )
    subject_profile = budget.subject_profile
    return BudgetRead(
        **{
            k: getattr(budget, k)
            for k in (
                "id", "org_id", "protocol_id", "name", "sponsor_name", "cro_name",
                "currency", "status", "drug_inn", "study_type", "n_enrolled",
                "screen_failure_rate_pct", "n_screen_failures", "n_alternates",
                "n_screened", "n_sites", "n_dosing_groups",
                "project_duration_months", "n_completed", "discount_type", "discount_value",
                "notes", "created_at", "updated_at",
            )
        },
        subject_profile=(subject_profile.value if subject_profile is not None else None),
        **{k: getattr(budget, k) for k in _PATIENT_PROFILE_FIELDS if k != "subject_profile"},
        derivation=budget.derivation or {},
        categories=[_category_read(c) for c in cats],
        rollup=BudgetRollup(**rollup),
    )


def _touch(budget: Budget) -> None:
    """onupdate=func.now() on Budget only fires when the budgets ROW itself is
    updated; mutating a child category/line-item does not touch it. Every
    child-mutating handler calls this explicitly before commit so the list
    view's "last modified" ordering doesn't go stale."""
    budget.updated_at = datetime.now(timezone.utc)


# ===========================================================================
# 1-2. List / create
# ===========================================================================


@router.get("", response_model=list[BudgetSummary], dependencies=[Depends(require_user)])
async def list_budgets(request: Request, db: AsyncSession = Depends(get_db)):
    actor = await _authenticate_actor(request, db)
    result = await db.execute(
        select(Budget)
        .options(selectinload(Budget.categories).selectinload(BudgetCategory.line_items))
        .where(Budget.org_id == actor.org_id)
        .order_by(Budget.updated_at.desc())
    )
    budgets = result.scalars().all()
    out: list[BudgetSummary] = []
    for b in budgets:
        rollup = budget_engine.compute_rollups(
            b.categories, discount_type=b.discount_type, discount_value=b.discount_value,
        )
        out.append(
            BudgetSummary(
                id=b.id,
                name=b.name,
                protocol_id=b.protocol_id,
                drug_inn=b.drug_inn,
                study_type=b.study_type,
                status=b.status,
                currency=b.currency,
                n_enrolled=b.n_enrolled,
                subject_profile=(b.subject_profile.value if b.subject_profile is not None else None),
                therapeutic_area=b.therapeutic_area,
                n_sites=b.n_sites,
                n_countries=b.n_countries,
                category_count=len(b.categories),
                line_item_count=sum(len(c.line_items) for c in b.categories),
                grand_total=rollup["grand_total"],
                created_at=b.created_at,
                updated_at=b.updated_at,
            )
        )
    return out


@router.post("", response_model=BudgetRead, status_code=201, dependencies=[Depends(require_user)])
async def create_budget(
    payload: BudgetCreate, request: Request, db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)

    protocol = None
    if payload.protocol_id:
        # 404 unknown / 403 other org — you cannot link another org's protocol.
        protocol = await get_protocol_scoped(db, payload.protocol_id, actor, load_drug=True)

    # A drug name alone is enough to generate a complete, fully-priced budget —
    # protocol-linking is an optional accuracy boost on top, never a blocking
    # either/or choice.
    drug_inn = (payload.drug_inn or "").strip() or (
        protocol.drug.inn if protocol is not None and protocol.drug else None
    )

    study_type = (payload.study_type or "").strip() or None
    if study_type is not None and study_type not in {st.value for st in StudyType}:
        raise HTTPException(
            422,
            detail=f"Unknown study_type '{study_type}'. Expected one of: "
                   + ", ".join(st.value for st in StudyType),
        )
    if study_type is None and protocol is not None and protocol.study_type:
        study_type = protocol.study_type.value

    _validate_subject_profile(payload.subject_profile)
    _validate_geography_profile(payload.geography_profile)
    if payload.dropout_rate_pct is not None and not (0 <= payload.dropout_rate_pct <= 100):
        raise HTTPException(422, detail="dropout_rate_pct must be between 0 and 100.")
    for field in ("startup_months", "recruitment_months", "treatment_months",
                  "followup_months", "closeout_months"):
        v = getattr(payload, field)
        if v is not None and v < 0:
            raise HTTPException(422, detail=f"{field} cannot be negative.")
    if payload.comparator_arm_fraction is not None and not (0 <= payload.comparator_arm_fraction <= 1):
        raise HTTPException(422, detail="comparator_arm_fraction must be between 0 and 1.")
    if payload.geography_factor is not None and not (0.05 <= payload.geography_factor <= 2.0):
        raise HTTPException(422, detail="geography_factor must be between 0.05 and 2.0.")

    name = (payload.name or "").strip()
    if not name:
        if not drug_inn:
            raise HTTPException(
                422,
                detail="Give the budget a drug name (drug_inn) or a name. A drug "
                       "name on its own is enough to generate a complete budget.",
            )
        name = f"{drug_inn.strip().title()} study budget"

    # org_id comes from the VERIFIED actor, never from the request body — a
    # client-supplied org_id would let any actor file a budget under an org
    # they don't belong to.
    budget = Budget(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        name=name,
        created_by=actor.id,
        sponsor_name=payload.sponsor_name,
        cro_name=payload.cro_name,
        currency=payload.currency or "USD",
        drug_inn=drug_inn,
        study_type=study_type,
        n_enrolled=payload.n_enrolled,
        n_sites=payload.n_sites,
        n_dosing_groups=payload.n_dosing_groups,
        project_duration_months=payload.project_duration_months,
        screen_failure_rate_pct=payload.screen_failure_rate_pct,
        n_alternates=payload.n_alternates,
        n_screened=payload.n_screened,
        n_screen_failures=payload.n_screen_failures,
        notes=payload.notes,
        derivation={},
        subject_profile=(BudgetSubjectProfile(payload.subject_profile) if payload.subject_profile else None),
        # Only the fields the caller actually SENT (not None) — several of
        # these columns (comparator_sourced_by_sponsor) are NOT NULL with a
        # server-side default, so explicitly passing None would insert NULL
        # instead of letting that default apply.
        **{
            k: getattr(payload, k) for k in _PATIENT_PROFILE_FIELDS
            if k != "subject_profile" and getattr(payload, k) is not None
        },
    )
    db.add(budget)

    # subject_profile must be resolved BEFORE seeding categories — a patient
    # budget and a healthy-volunteer budget seed structurally DIFFERENT
    # category sets (22 vs 17), almost entirely disjoint by name (spec §2).
    # This call is pure/read-only (resolve_subject_profile does not touch the
    # DB); auto_populate_budget below re-resolves everything for real,
    # idempotently arriving at the same answer since nothing has changed
    # between the two calls.
    resolved_profile, _ = budget_engine.resolve_subject_profile(budget, protocol, {})
    budget.categories.extend(
        budget_engine.seed_default_categories(budget.id, subject_profile=resolved_profile)
    )
    budget_engine.auto_populate_budget(budget, protocol)

    await db.commit()
    budget = await get_budget_scoped(db, budget.id, actor, load_tree=True)
    request.state.audit_resource_id = str(budget.id)
    return _budget_read(budget)


# ===========================================================================
# 2b. Profile preview / rate-card meta (spec §7.4) — declared BEFORE their
# sibling "/{budget_id}" GET route, same reason the /reorder routes below are
# declared before theirs: a UUID path param would otherwise never lose the
# match race against these two static sub-paths of the collection.
# ===========================================================================


@router.get(
    "/profile-preview", response_model=BudgetProfilePreview, dependencies=[Depends(require_user)],
)
async def profile_preview(
    drug_inn: str, request: Request, db: AsyncSession = Depends(get_db),
):
    """Cheap, read-only, no DB write, no AI call — powers the create-view
    hint. Runs the exact same resolution/pricing auto_populate_budget would,
    against a transient Budget that is never added to the session."""
    actor = await _authenticate_actor(request, db)
    drug_inn = (drug_inn or "").strip()

    tmp = Budget(
        id=uuid.uuid4(), org_id=actor.org_id, name="profile preview",
        created_by=actor.id, drug_inn=drug_inn, currency="USD", derivation={},
        # Transient (never added to the session), so SQLAlchemy's column
        # defaults never apply — set explicitly rather than relying on them.
        discount_type=BudgetDiscountType.none, discount_value=0.0,
        comparator_sourced_by_sponsor=False,
    )

    modality = classify_drug_modality(drug_inn)
    allowed = allowed_study_types(drug_inn)

    resolved_profile, profile_assumption = budget_engine.resolve_subject_profile(tmp, None, {})
    tmp.categories = budget_engine.seed_default_categories(tmp.id, subject_profile=resolved_profile)
    derivation = budget_engine.auto_populate_budget(tmp, None)
    rollup = budget_engine.compute_rollups(
        tmp.categories, discount_type=tmp.discount_type, discount_value=tmp.discount_value,
    )

    defaults = BudgetProfilePreviewDefaults(
        n_enrolled=tmp.n_enrolled, n_sites=tmp.n_sites, n_countries=tmp.n_countries,
        project_duration_months=tmp.project_duration_months,
        startup_months=tmp.startup_months, recruitment_months=tmp.recruitment_months,
        treatment_months=tmp.treatment_months, followup_months=tmp.followup_months,
        closeout_months=tmp.closeout_months, n_expected_saes=tmp.n_expected_saes,
        geography_profile=tmp.geography_profile,
    )

    return BudgetProfilePreview(
        drug_inn=drug_inn,
        modality=modality,
        allowed_study_types=list(allowed) if allowed else [st.value for st in StudyType],
        study_type=tmp.study_type or "",
        subject_profile=BudgetSubjectProfile(resolved_profile),
        subject_profile_source=profile_assumption["source"],
        subject_profile_basis=profile_assumption["basis"],
        therapeutic_area=tmp.therapeutic_area or "unknown",
        profile_key=derivation.get("profile_key"),
        defaults=defaults,
        estimated_grand_total=rollup["grand_total"],
        category_count=len(tmp.categories),
    )


@router.get(
    "/rate-card-meta", response_model=BudgetRateCardMeta, dependencies=[Depends(require_user)],
)
async def rate_card_meta(
    request: Request, subject_profile: str = "healthy", db: AsyncSession = Depends(get_db),
):
    """Static metadata for building UI without hardcoding the card. No auth
    beyond require_user (mirrors the module's role-gating rationale)."""
    await _authenticate_actor(request, db)
    _validate_subject_profile(subject_profile)
    is_patient = subject_profile == "patient"
    card = budget_rate_card.PATIENT_RATE_CARD if is_patient else budget_rate_card.RATE_CARD

    return BudgetRateCardMeta(
        subject_profile=BudgetSubjectProfile(subject_profile),
        rate_card_version=(
            budget_rate_card.PATIENT_RATE_CARD_VERSION if is_patient else budget_rate_card.RATE_CARD_VERSION
        ),
        currency=budget_rate_card.RATE_CARD_CURRENCY,
        disclaimer=budget_rate_card.PATIENT_DISCLAIMER if is_patient else budget_rate_card.DISCLAIMER,
        categories=[
            {"name": c.name, "bucket": c.bucket.value, "sort_order": c.sort_order, "line_count": len(c.lines)}
            for c in card
        ],
        buckets=sorted({c.bucket.value for c in card}),
        provenance_tiers=dict(budget_rate_card.PROVENANCE_TIERS) if is_patient else {},
        geography_profiles=dict(budget_rate_card.GEOGRAPHY_FACTORS) if is_patient else {},
    )


# ===========================================================================
# 3-5. Get / update / delete
# ===========================================================================


@router.get("/{budget_id}", response_model=BudgetRead, dependencies=[Depends(require_user)])
async def get_budget(
    budget_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    return _budget_read(budget)


@router.patch("/{budget_id}", response_model=BudgetRead, dependencies=[Depends(require_user)])
async def update_budget(
    budget_id: uuid.UUID,
    payload: BudgetUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)

    updates = payload.model_dump(exclude_unset=True)

    discount_type = updates.get("discount_type", budget.discount_type)
    discount_value = updates.get("discount_value", budget.discount_value)
    if discount_value is not None and discount_value < 0:
        raise HTTPException(422, detail="discount_value must be >= 0.")
    if discount_type == BudgetDiscountType.percent and not (0 <= discount_value <= 100):
        raise HTTPException(
            422, detail="A percentage discount must be between 0 and 100."
        )

    # Patient/oncology field validation (spec §7.3 — "same 422 rules" as create).
    # Range checks (dropout_rate_pct, comparator_arm_fraction, geography_factor,
    # phase-month/int non-negativity) already ran as pydantic field_validators
    # on BudgetUpdate; only the two enum-shaped fields need a router-side check
    # (kept plain str on the schema so the error text matches the spec exactly).
    if "subject_profile" in updates:
        _validate_subject_profile(updates["subject_profile"])
        if updates["subject_profile"] is not None:
            updates["subject_profile"] = BudgetSubjectProfile(updates["subject_profile"])
    if "geography_profile" in updates:
        _validate_geography_profile(updates["geography_profile"])

    for field, value in updates.items():
        setattr(budget, field, value)

    await db.commit()
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    return _budget_read(budget)


@router.delete("/{budget_id}", status_code=204, dependencies=[Depends(require_user)])
async def delete_budget(
    budget_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor)
    await db.delete(budget)
    await db.commit()
    request.state.audit_resource_id = str(budget_id)


# ===========================================================================
# 6. Link / re-link / unlink protocol
# ===========================================================================


@router.post(
    "/{budget_id}/link-protocol", response_model=BudgetRead, dependencies=[Depends(require_user)],
)
async def link_protocol(
    budget_id: uuid.UUID,
    payload: BudgetLinkProtocol,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Link, RE-link (re-derive after the protocol changed), or unlink.

    Re-derivation updates quantities in place by derivation_key and never
    overwrites a unit_cost/total the user typed.
    """
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)

    if payload.protocol_id is None:
        budget.protocol_id = None
        budget.derivation = {
            **(budget.derivation or {}),
            "unlinked_at": datetime.now(timezone.utc).isoformat(),
        }
        # line items deliberately untouched
    else:
        protocol = await get_protocol_scoped(db, payload.protocol_id, actor, load_drug=True)
        budget_engine.apply_protocol_derivation(budget, protocol)

    await db.commit()
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    return _budget_read(budget)


# ===========================================================================
# 6b. Re-price (spec §7.4) — re-runs auto-population in "reconcile" mode
# against the budget's CURRENT drivers, e.g. after a PATCH changed n_sites,
# a phase duration, or the discount. A plain PATCH does not move any total by
# itself (see BudgetUpdate's docstring note); this is how the frontend asks
# for that re-price explicitly.
# ===========================================================================


@router.post(
    "/{budget_id}/reprice", response_model=BudgetRead, dependencies=[Depends(require_user)],
)
async def reprice_budget(
    budget_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)

    protocol = None
    if budget.protocol_id:
        protocol = await get_protocol_scoped(db, budget.protocol_id, actor, load_drug=True)

    budget_engine.auto_populate_budget(budget, protocol, mode="reconcile")
    _touch(budget)
    await db.commit()
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    return _budget_read(budget)


# ===========================================================================
# 7. Rollup
# ===========================================================================


@router.get("/{budget_id}/rollup", response_model=BudgetRollup, dependencies=[Depends(require_user)])
async def get_rollup(
    budget_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    rollup = budget_engine.compute_rollups(
        budget.categories, discount_type=budget.discount_type, discount_value=budget.discount_value,
    )
    return BudgetRollup(**rollup)


# ===========================================================================
# 7b. Word (.docx) export
# ===========================================================================


@router.get("/{budget_id}/export/docx", dependencies=[Depends(require_user)])
async def export_budget_docx(
    budget_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
):
    """Export the budget as a .docx mirroring the real signed CRO budget
    documents: an Assumptions section, a Summary Study Budget table, and an
    Itemized Study Budget appendix (services/budget_docx_export.py)."""
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)   # SAME org guard as every other route
    rollup = budget_engine.compute_rollups(
        budget.categories,
        discount_type=budget.discount_type,
        discount_value=budget.discount_value,
    )
    docx_bytes = budget_docx_export.build_budget_docx(budget, rollup)

    slug = (budget.drug_inn or budget.name or "study").strip().replace(" ", "_")
    filename = f"budget_{slug}_{datetime.now(timezone.utc):%Y-%m-%d}.docx"

    db.add(AuditLog(
        id=uuid.uuid4(), user_id=actor.id, action="EXPORT:budget_docx",
        resource_type="budget", resource_id=str(budget_id),
        diff={"format": "docx", "filename": filename,
              "grand_total": rollup["grand_total"], "currency": budget.currency,
              "n_categories": len(budget.categories),
              "n_line_items": sum(len(c.line_items) for c in budget.categories),
              "rate_card_version": (budget.derivation or {}).get("rate_card_version"),
              "exported_at": datetime.now(timezone.utc).isoformat()},
        timestamp=datetime.now(timezone.utc),
    ))
    await db.commit()
    request.state.audit_resource_id = str(budget_id)

    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type=_DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": _content_disposition(filename)},
    )


# ===========================================================================
# 11 & 15. Reorders (declared BEFORE their sibling {id} routes — see module note)
# ===========================================================================


@router.post(
    "/{budget_id}/categories/reorder",
    response_model=list[BudgetCategoryRead],
    dependencies=[Depends(require_user)],
)
async def reorder_categories(
    budget_id: uuid.UUID,
    payload: ReorderRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)

    existing_ids = {c.id for c in budget.categories}
    if set(payload.ordered_ids) != existing_ids or len(payload.ordered_ids) != len(existing_ids):
        raise HTTPException(
            422, detail="ordered_ids must list every category of this budget exactly once."
        )

    by_id = {c.id: c for c in budget.categories}
    for idx, cat_id in enumerate(payload.ordered_ids):
        by_id[cat_id].sort_order = idx + 1
    _touch(budget)
    await db.commit()

    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    cats = sorted(budget.categories, key=lambda c: c.sort_order)
    return [_category_read(c) for c in cats]


@router.post(
    "/{budget_id}/categories/{category_id}/line-items/reorder",
    response_model=list[BudgetLineItemRead],
    dependencies=[Depends(require_user)],
)
async def reorder_line_items(
    budget_id: uuid.UUID,
    category_id: uuid.UUID,
    payload: ReorderRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    cat = await _get_category(db, budget, category_id)

    existing_ids = {li.id for li in cat.line_items}
    if set(payload.ordered_ids) != existing_ids or len(payload.ordered_ids) != len(existing_ids):
        raise HTTPException(
            422, detail="ordered_ids must list every line item of this category exactly once."
        )

    by_id = {li.id: li for li in cat.line_items}
    for idx, li_id in enumerate(payload.ordered_ids):
        by_id[li_id].sort_order = idx + 1
    _touch(budget)
    await db.commit()

    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    cat = await _get_category(db, budget, category_id)
    items = sorted(cat.line_items, key=lambda li: li.sort_order)
    return [BudgetLineItemRead.model_validate(li) for li in items]


# ===========================================================================
# 8-10. Categories
# ===========================================================================


@router.post(
    "/{budget_id}/categories",
    response_model=BudgetCategoryRead,
    status_code=201,
    dependencies=[Depends(require_user)],
)
async def create_category(
    budget_id: uuid.UUID,
    payload: BudgetCategoryCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)

    name = payload.name.strip()
    if not name:
        raise HTTPException(422, detail="Category name cannot be empty.")

    sort_order = payload.sort_order
    if sort_order is None:
        sort_order = max((c.sort_order for c in budget.categories), default=0) + 1

    cat = BudgetCategory(
        id=uuid.uuid4(),
        budget_id=budget.id,
        name=name,
        bucket=payload.bucket,
        note=payload.note,
        sort_order=sort_order,
        # Explicitly materialize the (empty) collection now. A relationship
        # attribute that is never assigned before commit is NOT already
        # "loaded" once the object becomes persistent — plain attribute
        # access after commit would issue a real SELECT for it, which is a
        # synchronous lazy load outside of an awaited context under the
        # async session (MissingGreenlet). Passing it here means
        # _category_read's access below is a plain in-memory read.
        line_items=[],
    )
    db.add(cat)
    _touch(budget)
    await db.commit()
    # NOTE: deliberately no db.refresh(cat) — see the line_items=[] comment
    # above; a refresh() would re-expire it and reintroduce the same issue,
    # and BudgetCategoryRead needs no server-generated column anyway (no
    # created_at/updated_at).
    request.state.audit_resource_id = str(cat.id)
    return _category_read(cat)


@router.patch(
    "/{budget_id}/categories/{category_id}",
    response_model=BudgetCategoryRead,
    dependencies=[Depends(require_user)],
)
async def update_category(
    budget_id: uuid.UUID,
    category_id: uuid.UUID,
    payload: BudgetCategoryUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    cat = await _get_category(db, budget, category_id)

    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates:
        stripped = (updates["name"] or "").strip()
        if not stripped:
            raise HTTPException(422, detail="Category name cannot be empty.")
        updates["name"] = stripped
    for field, value in updates.items():
        setattr(cat, field, value)

    _touch(budget)
    await db.commit()
    # No db.refresh(cat) — see create_category's note: cat.line_items is
    # already eagerly loaded (via get_budget_scoped(load_tree=True) above)
    # and a refresh would expire it, triggering an out-of-band lazy load.
    return _category_read(cat)


@router.delete(
    "/{budget_id}/categories/{category_id}", status_code=204, dependencies=[Depends(require_user)],
)
async def delete_category(
    budget_id: uuid.UUID,
    category_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    cat = await _get_category(db, budget, category_id)

    budget.categories.remove(cat)  # delete-orphan removes it and its lines
    _touch(budget)
    await db.commit()
    request.state.audit_resource_id = str(category_id)


# ===========================================================================
# 12-14. Line items
# ===========================================================================


@router.post(
    "/{budget_id}/categories/{category_id}/line-items",
    response_model=BudgetLineItemRead,
    status_code=201,
    dependencies=[Depends(require_user)],
)
async def create_line_item(
    budget_id: uuid.UUID,
    category_id: uuid.UUID,
    payload: BudgetLineItemCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    cat = await _get_category(db, budget, category_id)

    label = payload.label.strip()
    if not label:
        raise HTTPException(422, detail="Line item label cannot be empty.")

    # TOTAL RULE: an explicitly supplied total (including an explicit null)
    # always wins; the server never overrides a lump sum. Only when the field
    # was omitted entirely, and both quantity and unit_cost are present, does
    # the server compute it.
    if "total" not in payload.model_fields_set and payload.quantity is not None and payload.unit_cost is not None:
        total = budget_engine.line_total_from_units(payload.quantity, payload.unit_cost)
    else:
        total = payload.total

    sort_order = payload.sort_order
    if sort_order is None:
        sort_order = max((li.sort_order for li in cat.line_items), default=0) + 1

    li = BudgetLineItem(
        id=uuid.uuid4(),
        category_id=cat.id,
        label=label,
        quantity=payload.quantity,
        unit_cost=payload.unit_cost,
        unit_description=payload.unit_description,
        total=total,
        status=payload.status,
        # FORCED — only the engine may mint a protocol_derived row; a client
        # cannot fake the "the system found this for you" badge.
        source=BudgetLineSource.manual,
        derivation_key=None,
        note=payload.note,
        sort_order=sort_order,
    )
    db.add(li)
    _touch(budget)
    await db.commit()
    await db.refresh(li)
    request.state.audit_resource_id = str(li.id)
    return BudgetLineItemRead.model_validate(li)


@router.patch(
    "/{budget_id}/line-items/{line_item_id}",
    response_model=BudgetLineItemRead,
    dependencies=[Depends(require_user)],
)
async def update_line_item(
    budget_id: uuid.UUID,
    line_item_id: uuid.UUID,
    payload: BudgetLineItemUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Flat under the budget (not the category) so MOVING a row between
    categories is just a field change."""
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    li = await _get_line_item(db, budget, line_item_id)

    updates = payload.model_dump(exclude_unset=True)

    if "label" in updates:
        stripped = (updates["label"] or "").strip()
        if not stripped:
            raise HTTPException(422, detail="Line item label cannot be empty.")
        updates["label"] = stripped

    if "category_id" in updates:
        new_category_id = updates.pop("category_id")
        target_cat = await _get_category(db, budget, new_category_id)
        li.category_id = target_cat.id

    # TOTAL RULE: recompute ONLY when "total" was not sent AND ("quantity" or
    # "unit_cost" was sent) AND both merged values are non-null. Otherwise
    # leave total exactly as stored/supplied.
    if "total" not in updates and ("quantity" in updates or "unit_cost" in updates):
        merged_quantity = updates.get("quantity", li.quantity)
        merged_unit_cost = updates.get("unit_cost", li.unit_cost)
        if merged_quantity is not None and merged_unit_cost is not None:
            updates["total"] = budget_engine.line_total_from_units(merged_quantity, merged_unit_cost)

    # `source` / `derivation_key` are NOT patchable — ignored if sent (a
    # protocol_derived row keeps its badge even after the user prices it).
    updates.pop("source", None)
    updates.pop("derivation_key", None)

    for field, value in updates.items():
        setattr(li, field, value)

    _touch(budget)
    await db.commit()
    await db.refresh(li)
    return BudgetLineItemRead.model_validate(li)


@router.delete(
    "/{budget_id}/line-items/{line_item_id}", status_code=204, dependencies=[Depends(require_user)],
)
async def delete_line_item(
    budget_id: uuid.UUID,
    line_item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    budget = await get_budget_scoped(db, budget_id, actor, load_tree=True)
    li = await _get_line_item(db, budget, line_item_id)

    li.category.line_items.remove(li)
    _touch(budget)
    await db.commit()
    request.state.audit_resource_id = str(line_item_id)


# ---------------------------------------------------------------------------
# Shared private helpers
# ---------------------------------------------------------------------------


async def _get_category(db: AsyncSession, budget: Budget, category_id: uuid.UUID) -> BudgetCategory:
    """Category fetched SCOPED TO ITS PARENT BUDGET so an id belonging to
    another budget (possibly another org's) cannot be reached through this
    one — the same guard comments.py::_get_comment_or_404 applies. Operates
    on the eagerly-loaded tree, so no extra query and no chance of reaching
    outside the scoped budget."""
    cat = next((c for c in budget.categories if c.id == category_id), None)
    if not cat:
        raise HTTPException(404, detail="Budget category not found.")
    return cat


async def _get_line_item(db: AsyncSession, budget: Budget, line_item_id: uuid.UUID) -> BudgetLineItem:
    for c in budget.categories:
        for li in c.line_items:
            if li.id == line_item_id:
                return li
    raise HTTPException(404, detail="Budget line item not found.")
