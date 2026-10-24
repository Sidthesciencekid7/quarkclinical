from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth
from auth import require_user
from database import get_db
from dependencies import authenticate_actor, require_admin
from models import User, UserRole
from schemas import UserRead

router = APIRouter(prefix="/api/users", tags=["users"])


class CapabilitiesRead(BaseModel):
    """What the signed-in user may do, for the UI to show/hide controls.

    Advisory only - every action is still enforced server-side by its own route.
    """
    role: UserRole
    is_org_owner: bool
    is_admin: bool
    can_generate: bool


@router.get("/me", response_model=UserRead, dependencies=[Depends(require_user)])
async def current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return the users-table row the backend resolved the caller to.

    With Clerk auth on, this is derived from the VERIFIED session (Clerk email →
    users row), so the frontend can display the real, spoof-proof identity
    instead of trusting the client-side "acting as" picker. A valid session
    whose email isn't provisioned yields 403 (see authenticate_actor).
    """
    return await authenticate_actor(request, db)


# Registered before any "/{user_id}"-style route, so "me/capabilities" can
# never be captured as a user id.
@router.get(
    "/me/capabilities",
    response_model=CapabilitiesRead,
    dependencies=[Depends(require_user)],
)
async def my_capabilities(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """What the caller can do - role, org-owner, admin, and whether they can
    generate - derived from the verified actor (never a client-supplied value).

    ``is_admin`` is exactly the existing /admin gate (dependencies.require_admin):
    the verified session's email must be in ADMIN_EMAILS, and, as for the admin
    routes themselves, it is True for everyone when Clerk auth is off (local dev
    and tests have no session to check an email against).

    ``can_generate`` is the author-role gate used by protocol/budget creation
    (role == generator). It does not look at the license: a pending license is
    reported separately by GET /api/orgs/me, and enforced (402) where credits are
    spent.
    """
    actor = await authenticate_actor(request, db)
    try:
        await require_admin(request)
        is_admin = True
    except HTTPException:
        is_admin = False
    return CapabilitiesRead(
        role=actor.role,
        is_org_owner=bool(actor.is_org_owner),
        is_admin=is_admin,
        can_generate=actor.role == UserRole.generator,
    )


@router.get("/", response_model=list[UserRead], dependencies=[Depends(require_user)])
async def list_users(
    request: Request,
    org_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """List users (id/org/email/role only, never hashed_password).

    Used by the frontend's local-dev "acting as" picker to bootstrap the list
    of seeded identities it can send as X-User-Id. In Clerk/production mode the
    frontend does NOT call this (it resolves identity via GET /users/me only —
    see lib/actingUser.tsx).

    SECURITY (cross-org isolation): when Clerk auth is ON, results are scoped
    to the CALLER'S OWN org, derived from the verified session (authenticate_actor)
    — the client-supplied ``org_id`` query param is ignored, and an
    unprovisioned session gets 403. Without this scoping, any Clerk signup (the
    /app sign-up flow is public) could call this to enumerate every customer's
    email/role/org across ALL organizations — a real PII-harvesting +
    cross-tenant leak. When auth is OFF (local dev/tests — no session to scope
    to), the picker still needs to bootstrap before any identity is selected,
    so the open (optionally org_id-filtered) list is retained there only.
    """
    if auth.AUTH_ENABLED:
        actor = await authenticate_actor(request, db)
        result = await db.execute(
            select(User).where(User.org_id == actor.org_id).order_by(User.email)
        )
        return result.scalars().all()

    stmt = select(User)
    if org_id:
        stmt = stmt.where(User.org_id == org_id)
    result = await db.execute(stmt.order_by(User.email))
    return result.scalars().all()
