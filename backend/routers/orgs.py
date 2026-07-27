"""Organization / license / team-provisioning endpoints.

Deliberately Stripe-free — enterprise deals here are sales-assisted, not
self-serve checkout. Self-serve org creation (POST /) still works so a
prospect can immediately see the product and set up their team structure,
but the resulting License starts "pending", not "active": generation stays
blocked (see dependencies.require_active_license) until the operator
manually activates it via POST /{org_id}/activate-license once a deal is
confirmed out-of-band (wire transfer, invoice, whatever — outside this app).
GET /admin/list is the operator's own view of every org + license status to
find who to activate; both admin routes are gated by dependencies.require_admin
(a fixed ADMIN_EMAILS allowlist — there's no general admin-role system).
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import auth
from database import get_db
from dependencies import _admin_emails, audit_logger, authenticate_actor, require_admin
from models import License, LicenseStatus, Organization, User, UserRole
from schemas import (
    AdminOrgRead,
    LicenseActivateRequest,
    LicenseRead,
    MemberRead,
    OrgCreate,
    OrgJoinRequest,
    OrgRead,
)
from security import hash_password

router = APIRouter(prefix="/api/orgs", tags=["orgs"], dependencies=[Depends(audit_logger)])

DEFAULT_SEAT_COUNT = 20


def _generate_join_token() -> str:
    return secrets.token_urlsafe(24)


def _slugify(name: str) -> str:
    base = "-".join(name.strip().lower().split())[:100] or "org"
    return f"{base}-{secrets.token_hex(4)}"


async def _resolve_new_user_email(request: Request, payload_email: Optional[str]) -> str:
    """The email a brand-new user row should be provisioned under.

    With Clerk auth on, this is the VERIFIED session email (same resolution
    dependencies.authenticate_actor uses for existing users) — any email in
    the request body is ignored, so nobody can provision an account under an
    address they don't control. With Clerk auth off (local dev/tests — no
    session to bind to), there's no verified email available, so the caller
    must supply one explicitly; this mirrors the existing X-User-Id dev-mode
    trust boundary used elsewhere in this app and is never reached when auth
    is actually on.
    """
    if auth.AUTH_ENABLED:
        claims = getattr(request.state, "clerk_claims", None)
        if not claims:
            raise HTTPException(401, detail="Authentication required.")
        email = claims.get("email") or claims.get("email_address")
        if not email:
            email = await auth.clerk_email_for_sub(claims.get("sub", ""))
        if not email:
            raise HTTPException(401, detail="Could not resolve your account email from the session.")
        return email.lower()

    if not payload_email or not payload_email.strip():
        raise HTTPException(422, detail="email is required when Clerk auth is disabled.")
    return payload_email.strip().lower()


async def _existing_user_for_email(db: AsyncSession, email: str) -> Optional[User]:
    result = await db.execute(select(User).where(func.lower(User.email) == email.lower()))
    return result.scalar_one_or_none()


def _require_org_owner(actor: User) -> None:
    if not actor.is_org_owner:
        raise HTTPException(403, detail="Only the organization owner can do this.")


async def _get_org_by_slug(db: AsyncSession, slug: str) -> Organization:
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(404, detail="No organization found for your account.")
    return org


async def _latest_license(db: AsyncSession, organization_id: uuid.UUID) -> Optional[License]:
    result = await db.execute(
        select(License).where(License.organization_id == organization_id).order_by(License.created_at.desc())
    )
    return result.scalars().first()


async def _seats_used(db: AsyncSession, slug: str) -> int:
    result = await db.execute(select(func.count()).select_from(User).where(User.org_id == slug))
    return result.scalar_one()


async def _org_read(db: AsyncSession, org: Organization, *, is_owner: bool, include_token: bool) -> OrgRead:
    lic = await _latest_license(db, org.id)
    used = await _seats_used(db, org.slug)
    license_read = (
        LicenseRead(
            status=lic.status.value, seat_count=lic.seat_count, seats_used=used,
            period_start=lic.period_start, period_end=lic.period_end,
        )
        if lic else None
    )
    return OrgRead(
        id=org.id, name=org.name, slug=org.slug, is_owner=is_owner,
        license=license_read, join_token=org.join_token if include_token else None,
    )


@router.post("/", response_model=OrgRead, status_code=201, dependencies=[Depends(auth.require_user)])
async def create_org(payload: OrgCreate, request: Request, db: AsyncSession = Depends(get_db)):
    """Self-serve org creation — the caller becomes its owner. The License
    starts "pending" (not active): the org and its team structure exist
    immediately, but protocol generation stays blocked (see
    dependencies.require_active_license) until the operator manually
    activates it via POST /{org_id}/activate-license once a sales-assisted
    deal is confirmed (see module docstring — no Stripe). An ADMIN_EMAILS
    operator's own org is created active outright — "admin access" means
    full access to the product, not just the /admin endpoints, and the
    frontend's pending-license screen has no separate way to know a viewer
    is an operator, so this keeps frontend and backend consistent. Rejects
    if this email already belongs to a different org.
    """
    email = await _resolve_new_user_email(request, payload.email)
    if await _existing_user_for_email(db, email):
        raise HTTPException(409, detail=f"{email} is already part of an organization.")

    name = payload.name.strip()
    if not name:
        raise HTTPException(422, detail="Organization name is required.")

    now = datetime.now(timezone.utc)
    org = Organization(
        id=uuid.uuid4(), slug=_slugify(name), name=name,
        join_token=_generate_join_token(), created_at=now,
    )
    db.add(org)

    initial_status = LicenseStatus.active if email.lower() in _admin_emails() else LicenseStatus.pending
    db.add(License(
        id=uuid.uuid4(), organization_id=org.id, status=initial_status,
        seat_count=DEFAULT_SEAT_COUNT, period_start=now, period_end=now + timedelta(days=365),
    ))

    db.add(User(
        id=uuid.uuid4(), org_id=org.slug, email=email, role=UserRole.generator,
        hashed_password=hash_password(uuid.uuid4().hex), is_org_owner=True,
    ))

    await db.commit()
    request.state.audit_resource_id = str(org.id)
    return await _org_read(db, org, is_owner=True, include_token=True)


@router.post("/join", response_model=OrgRead, status_code=201, dependencies=[Depends(auth.require_user)])
async def join_org(payload: OrgJoinRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Redeem an org's join link/token — creates the caller as a new seat on
    that org's license. Rejects an invalid token, a full-seats org, a
    non-active license, or an email that's already provisioned elsewhere.
    """
    email = await _resolve_new_user_email(request, payload.email)
    if await _existing_user_for_email(db, email):
        raise HTTPException(409, detail=f"{email} is already part of an organization.")

    token = (payload.token or "").strip()
    if not token:
        raise HTTPException(422, detail="token is required.")

    result = await db.execute(select(Organization).where(Organization.join_token == token))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(404, detail="Invalid or expired invite link.")

    lic = await _latest_license(db, org.id)
    if not lic or lic.status != LicenseStatus.active:
        raise HTTPException(403, detail="This organization's license is not active.")

    used = await _seats_used(db, org.slug)
    if used >= lic.seat_count:
        raise HTTPException(403, detail="This organization has no seats available — ask your admin to free one up.")

    db.add(User(
        id=uuid.uuid4(), org_id=org.slug, email=email, role=UserRole.generator,
        hashed_password=hash_password(uuid.uuid4().hex), is_org_owner=False,
    ))
    await db.commit()

    request.state.audit_resource_id = str(org.id)
    return await _org_read(db, org, is_owner=False, include_token=False)


@router.get("/me", response_model=OrgRead, dependencies=[Depends(auth.require_user)])
async def get_my_org(request: Request, db: AsyncSession = Depends(get_db)):
    """The caller's own organization: name, license status/seats, and (owner
    only) the join link/token."""
    actor = await authenticate_actor(request, db)
    org = await _get_org_by_slug(db, actor.org_id)
    return await _org_read(db, org, is_owner=actor.is_org_owner, include_token=actor.is_org_owner)


@router.post("/rotate-join-token", response_model=OrgRead, dependencies=[Depends(auth.require_user)])
async def rotate_join_token(request: Request, db: AsyncSession = Depends(get_db)):
    """Owner-only: regenerate the org's join link/token, invalidating the old
    one immediately."""
    actor = await authenticate_actor(request, db)
    _require_org_owner(actor)
    org = await _get_org_by_slug(db, actor.org_id)
    org.join_token = _generate_join_token()
    await db.commit()
    request.state.audit_resource_id = str(org.id)
    return await _org_read(db, org, is_owner=True, include_token=True)


@router.get("/members", response_model=list[MemberRead], dependencies=[Depends(auth.require_user)])
async def list_members(request: Request, db: AsyncSession = Depends(get_db)):
    """The caller's own org's team roster — scoped server-side to
    ``actor.org_id`` (never a client-supplied org filter), so one org can
    never enumerate another's members."""
    actor = await authenticate_actor(request, db)
    result = await db.execute(select(User).where(User.org_id == actor.org_id).order_by(User.email))
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Admin (operator-only — ADMIN_EMAILS allowlist, see dependencies.require_admin)
# ---------------------------------------------------------------------------


@router.get("/admin/list", response_model=list[AdminOrgRead], dependencies=[Depends(auth.require_user)])
async def admin_list_orgs(request: Request, db: AsyncSession = Depends(get_db)):
    """Every org + license status/seats + owner email, newest first — the
    operator's own view for finding which pending org to activate after a
    sales conversation closes."""
    await require_admin(request)
    result = await db.execute(select(Organization).order_by(Organization.created_at.desc()))
    orgs = result.scalars().all()

    now = datetime.now(timezone.utc)
    out: list[AdminOrgRead] = []
    for org in orgs:
        lic = await _latest_license(db, org.id)
        used = await _seats_used(db, org.slug)
        owner_result = await db.execute(
            select(User).where(User.org_id == org.slug, User.is_org_owner.is_(True))
        )
        owner = owner_result.scalar_one_or_none()
        days_remaining = (
            (lic.period_end - now).days if lic and lic.status == LicenseStatus.active else None
        )
        out.append(AdminOrgRead(
            id=org.id, name=org.name, slug=org.slug,
            owner_email=owner.email if owner else None,
            license_status=lic.status.value if lic else None,
            seat_count=lic.seat_count if lic else None,
            seats_used=used,
            days_remaining=days_remaining,
            created_at=org.created_at,
        ))
    return out


@router.post(
    "/{org_id}/activate-license", response_model=OrgRead, dependencies=[Depends(auth.require_user)],
)
async def activate_license(
    org_id: uuid.UUID, payload: LicenseActivateRequest, request: Request, db: AsyncSession = Depends(get_db),
):
    """Manually flip an org's license to active — the no-Stripe stand-in for
    a payment webhook. Sets a fresh period starting now; ``seat_count``
    defaults to whatever the org already has (self-serve default) unless
    overridden here to match the actual deal size."""
    await require_admin(request)
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(404, detail="Organization not found.")

    now = datetime.now(timezone.utc)
    lic = await _latest_license(db, org.id)
    if lic:
        lic.status = LicenseStatus.active
        lic.period_start = now
        lic.period_end = now + timedelta(days=payload.period_days)
        if payload.seat_count is not None:
            lic.seat_count = payload.seat_count
    else:
        db.add(License(
            id=uuid.uuid4(), organization_id=org.id, status=LicenseStatus.active,
            seat_count=payload.seat_count or DEFAULT_SEAT_COUNT,
            period_start=now, period_end=now + timedelta(days=payload.period_days),
        ))

    await db.commit()
    request.state.audit_resource_id = str(org.id)
    return await _org_read(db, org, is_owner=True, include_token=True)


@router.post(
    "/{org_id}/suspend-license", response_model=OrgRead, dependencies=[Depends(auth.require_user)],
)
async def suspend_license(org_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """The operator's kill switch — disables an org's access without
    deleting anything. Blocks protocol generation (require_active_license)
    and new joins (join_org already rejects any non-active license) until
    re-activated via POST /{org_id}/activate-license. Does not touch
    seat_count or the join token — only status."""
    await require_admin(request)
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(404, detail="Organization not found.")

    lic = await _latest_license(db, org.id)
    if not lic:
        raise HTTPException(404, detail="This organization has no license to suspend.")

    lic.status = LicenseStatus.suspended
    await db.commit()
    request.state.audit_resource_id = str(org.id)
    return await _org_read(db, org, is_owner=True, include_token=True)
