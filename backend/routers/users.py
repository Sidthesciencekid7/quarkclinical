from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import auth
from auth import require_user
from database import get_db
from dependencies import authenticate_actor
from models import User
from schemas import UserRead

router = APIRouter(prefix="/api/users", tags=["users"])


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
