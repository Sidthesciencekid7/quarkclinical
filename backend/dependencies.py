"""Shared FastAPI dependencies.

audit_logger — yielded dependency that writes one audit_logs entry per
mutating request (POST/PATCH/PUT/DELETE) and per GET on a specific resource.
Reads the actor identity from the X-User-Id header (auth stub — replace with
JWT extraction once auth is wired).

authenticate_actor / get_protocol_scoped — shared identity + org-isolation
helpers used by protocols.py, export.py, and audit.py so every protocol-
scoped endpoint enforces "same org as the acting user" the same way sign_
protocol already did (see B1/B3 audit notes) instead of each router
reimplementing (or forgetting) the check.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import AsyncSessionLocal
from models import (
    AuditLog,
    License,
    LicenseStatus,
    Organization,
    Protocol,
    StudyReport,
    User,
)

# Map URL path segment → resource_type label stored in audit_logs
_PATH_TO_RESOURCE: dict[str, str] = {
    "drugs": "drug",
    "literature": "literature",
    "protocols": "protocol",
    "export": "export",
    "audit": "audit",
    "orgs": "organization",
}

_LOG_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


def _parse_path(path: str) -> tuple[str, Optional[str]]:
    """Derive (resource_type, resource_id) from a request path.

    Examples:
        /api/protocols/<uuid>/sections/title → ("protocol", "<uuid>")
        /api/drugs                           → ("drug", None)
        /api/export/docx                     → ("export", None)
    """
    parts = [p for p in path.strip("/").split("/") if p]
    # Expected shape: ["api", "<resource>", "<id-or-subpath>", ...]
    if len(parts) < 2:
        return "unknown", None

    resource_type = _PATH_TO_RESOURCE.get(parts[1], parts[1])

    resource_id: Optional[str] = None
    if len(parts) > 2:
        try:
            uuid.UUID(parts[2])
            resource_id = parts[2]
        except ValueError:
            pass

    return resource_type, resource_id


async def audit_logger(request: Request):
    """FastAPI yielded dependency — logs every qualifying request to audit_logs.

    Usage (apply to an entire router):
        router = APIRouter(dependencies=[Depends(audit_logger)])

    Logs: POST, PATCH, PUT, DELETE on any route, plus GET on specific resources
    (where a UUID is the third path segment).  GET list/search endpoints are
    intentionally excluded to keep the audit trail focused on data changes and
    individual record access.
    """
    user_id_str = request.headers.get("X-User-Id")
    ip = request.client.host if request.client else None
    resource_type, resource_id = _parse_path(request.url.path)
    action = f"{request.method}:{resource_type}"

    yield  # ← route handler executes here

    # A creation POST (e.g. POST /api/protocols/) has no id in the URL, so
    # _parse_path can't find one — the id doesn't exist until the handler
    # creates it. A handler that wants the real id in the audit trail sets
    # request.state.audit_resource_id once it knows it (see create_protocol).
    resource_id = getattr(request.state, "audit_resource_id", None) or resource_id

    should_log = request.method in _LOG_METHODS or (
        request.method == "GET" and resource_id is not None
    )
    if not should_log:
        return

    # INTEGRITY: prefer the VERIFIED actor id (stashed by authenticate_actor
    # from the Clerk session) over the client-supplied X-User-Id header. When
    # Clerk auth is on, X-User-Id is spoofable and must not be trusted for
    # audit attribution — otherwise an actor could falsify who the trail says
    # performed an action (a 21 CFR Part 11 concern). Handlers that resolve the
    # actor set request.state.actor_id; the header is only the fallback for
    # dev/no-auth mode (where it IS the identity source anyway).
    verified_actor_id = getattr(request.state, "actor_id", None)
    if verified_actor_id is not None:
        user_uuid: Optional[uuid.UUID] = verified_actor_id
    else:
        try:
            user_uuid = uuid.UUID(user_id_str) if user_id_str else None
        except ValueError:
            user_uuid = None

    try:
        async with AsyncSessionLocal() as session:
            session.add(
                AuditLog(
                    id=uuid.uuid4(),
                    user_id=user_uuid,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    ip_address=ip,
                    timestamp=datetime.now(timezone.utc),
                )
            )
            await session.commit()
    except Exception:
        # Never let audit write failure mask the route response or error.
        pass


async def _actor_from_x_user_id(request: Request, db: AsyncSession) -> User:
    """Legacy/local resolution: trust the X-User-Id header. Used ONLY when Clerk
    auth is disabled (auth.AUTH_ENABLED False) — i.e. local dev and the test
    suite, where there's no Clerk session to bind to. Never used when auth is on.
    """
    raw = request.headers.get("X-User-Id")
    if not raw:
        raise HTTPException(401, detail="X-User-Id header is required.")
    try:
        user_id = uuid.UUID(raw)
    except ValueError:
        raise HTTPException(401, detail="X-User-Id header is not a valid UUID.")
    actor = await db.get(User, user_id)
    if not actor:
        raise HTTPException(401, detail="User not found in users table.")
    return actor


async def authenticate_actor(request: Request, db: AsyncSession) -> User:
    """Resolve the acting user to a real users-table row, or 401/403.

    SECURITY (the fix for the critical "spoofable identity" finding): when Clerk
    auth is enabled, the actor is derived from the VERIFIED Clerk session — the
    signed JWT's ``sub`` → primary email (auth.clerk_email_for_sub) → the
    users-table row with that email. The client-supplied X-User-Id header is
    IGNORED for identity in this mode, so it can no longer be used to impersonate
    another user or cross an org boundary. A Clerk account whose email isn't
    provisioned in the users table gets 403 (not 401) — the session is valid,
    the person just isn't authorized in this app yet.

    When Clerk auth is disabled (auth.AUTH_ENABLED False — local dev + tests),
    there's no session to bind to, so it falls back to the X-User-Id header.
    """
    # Import here (not at module load) so tests that monkeypatch the auth flags
    # see the current value, and to avoid a circular import at startup.
    import auth

    if not auth.AUTH_ENABLED:
        actor = await _actor_from_x_user_id(request, db)
        request.state.actor_id = actor.id  # verified-actor hint for audit_logger
        return actor

    claims = getattr(request.state, "clerk_claims", None)
    if not claims:
        # require_user (which sets this) must run before any route that calls us;
        # absence means the caller reached here without a verified session.
        raise HTTPException(401, detail="Authentication required.")

    # Prefer an email claim if the Clerk JWT template provides one; else resolve
    # via the Clerk Backend API (cached).
    email = claims.get("email") or claims.get("email_address")
    if not email:
        email = await auth.clerk_email_for_sub(claims.get("sub", ""))
    if not email:
        raise HTTPException(
            401, detail="Could not resolve your account email from the session."
        )

    # Case-insensitive match on both sides — Clerk emails may be mixed-case.
    result = await db.execute(
        select(User).where(func.lower(User.email) == email.lower())
    )
    actor = result.scalar_one_or_none()
    if not actor:
        raise HTTPException(
            403,
            detail=(
                f"Your account ({email}) is not provisioned in this workspace. "
                "Ask an administrator to add you."
            ),
        )
    # Bind the verified identity to the request so audit_logger attributes the
    # trail to the real actor, never the spoofable X-User-Id header.
    request.state.actor_id = actor.id
    return actor


async def get_protocol_scoped(
    db: AsyncSession, protocol_id: uuid.UUID, actor: User, *, load_drug: bool = False,
) -> Protocol:
    """Fetch a protocol, 404 if missing, 403 if it belongs to a different org
    than the actor's. A customer must never be able to read, edit, export, or
    audit-trail-inspect another customer's protocol just by guessing/holding
    its UUID — every protocol-scoped route should fetch through this instead
    of a bare db.get(Protocol, ...).
    """
    stmt = select(Protocol).where(Protocol.id == protocol_id)
    if load_drug:
        stmt = stmt.options(selectinload(Protocol.drug))
    result = await db.execute(stmt)
    protocol = result.scalar_one_or_none()
    if not protocol:
        raise HTTPException(404, detail="Protocol not found.")
    if protocol.org_id != actor.org_id:
        raise HTTPException(
            403, detail="Protocol belongs to a different organization."
        )
    return protocol


async def get_study_report_scoped(
    db: AsyncSession, study_report_id: uuid.UUID, actor: User,
) -> StudyReport:
    """Fetch a study report (CSR), 404 if missing, 403 if it belongs to a
    different org than the actor's — the same IDOR guard as get_protocol_scoped.
    A CSR carries real (aggregate) clinical results, so cross-org reads must be
    impossible even with a guessed/held UUID; every CSR-scoped route fetches
    through this instead of a bare db.get(StudyReport, ...).
    """
    result = await db.execute(
        select(StudyReport).where(StudyReport.id == study_report_id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, detail="Study report not found.")
    if report.org_id != actor.org_id:
        raise HTTPException(
            403, detail="Study report belongs to a different organization."
        )
    return report


def _admin_emails() -> set[str]:
    """Operator allowlist for internal sales/ops endpoints (lead list, manual
    license activation). No general admin-role system exists — this is a
    fixed set of emails from the ADMIN_EMAILS env var (comma-separated),
    deliberately minimal since there's no Stripe/billing automation to gate
    instead."""
    raw = os.getenv("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


async def require_admin(request: Request) -> str:
    """Raise 403 unless the caller's verified email is in ADMIN_EMAILS.

    With Clerk auth off (local dev/tests — no session to check an email
    against), this is a no-op so tests can exercise admin routes via the
    normal X-User-Id path; it's never reached in that state in production
    since auth.AUTH_ENABLED is always true there.
    """
    import auth  # local import — see authenticate_actor's note on circular imports

    if not auth.AUTH_ENABLED:
        return "dev-admin"

    claims = getattr(request.state, "clerk_claims", None)
    if not claims:
        raise HTTPException(401, detail="Authentication required.")
    email = claims.get("email") or claims.get("email_address")
    if not email:
        email = await auth.clerk_email_for_sub(claims.get("sub", ""))
    if not email or email.lower() not in _admin_emails():
        raise HTTPException(403, detail="Admin access required.")
    return email.lower()


async def require_active_license(db: AsyncSession, actor: User) -> None:
    """Blocks protocol generation unless the actor's org has an active
    License — the manual, no-Stripe stand-in for a payment gate. Self-serve
    orgs (routers/orgs.py::create_org) start "pending"; the operator flips
    them to "active" via POST /api/orgs/{id}/activate-license once a
    sales-assisted deal is confirmed out-of-band.

    Orgs with no matching Organization row at all are NOT blocked — every
    org_id that existed before this feature was backfilled with an active
    License in migration 004, so a missing row here means something else is
    going on (e.g. a test fixture), not a real trial-abuse path; failing
    open avoids accidentally locking out an existing paying customer over an
    ops edge case.

    Operators (ADMIN_EMAILS) are exempt outright — "admin access" is meant
    to mean full access to the product, not just the /admin endpoints,
    regardless of whatever license state their own personal org happens to
    be in.
    """
    if actor.email.lower() in _admin_emails():
        return

    result = await db.execute(select(Organization).where(Organization.slug == actor.org_id))
    org = result.scalar_one_or_none()
    if not org:
        return

    lic_result = await db.execute(
        select(License).where(License.organization_id == org.id).order_by(License.created_at.desc())
    )
    lic = lic_result.scalars().first()
    if not lic or lic.status != LicenseStatus.active:
        raise HTTPException(
            402,
            detail=(
                "Your organization's license isn't active yet. "
                "We'll activate it as soon as your sales conversation is confirmed — "
                "reach out if you're still waiting to hear back."
            ),
        )


# ---------------------------------------------------------------------------
# Lightweight in-memory rate limiting
# ---------------------------------------------------------------------------
#
# Best-effort abuse mitigation for public / cost-bearing endpoints (lead spam,
# unauthenticated compute, credit-spending generation). NOT a hard security
# control: it's per-process, and this Railway service now runs multiple Uvicorn
# workers (Procfile/Dockerfile --workers), so each worker keeps its own
# independent buckets — a client's real ceiling is closer to
# (max_calls * worker count) than the configured number. A shared store (e.g.
# Redis) would make this exact, but isn't worth the added infrastructure at
# current traffic given these limits are deliberately generous (a normal human
# never trips them) and this isn't an access-control boundary. It's also keyed
# on a client-appendable X-Forwarded-For for the same reason. Revisit if either
# assumption stops holding.

_RATE_BUCKETS: dict[str, deque[float]] = {}
_RATE_BUCKETS_MAX_KEYS = 10_000  # bound memory across many distinct clients


def _client_key(request: Request) -> str:
    """Best-effort client identity for rate limiting. Behind Railway's proxy,
    request.client.host is the proxy, so the real client is the first hop in
    X-Forwarded-For. Spoofable, but this is anti-abuse, not access control."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_check(key: str, max_calls: int, window_seconds: float) -> None:
    """Sliding-window check. Raises HTTPException(429) if ``key`` has already
    made ``max_calls`` within the trailing ``window_seconds``. Separated from
    the dependency wrapper so the logic is unit-testable without the
    under-pytest short-circuit. No ``await`` inside → atomic on the event loop."""
    now = time.monotonic()
    bucket = _RATE_BUCKETS.get(key)
    if bucket is None:
        if len(_RATE_BUCKETS) >= _RATE_BUCKETS_MAX_KEYS:
            # Crude bound: drop an arbitrary existing bucket. Under sustained
            # abuse from many IPs this degrades gracefully rather than OOMing.
            _RATE_BUCKETS.pop(next(iter(_RATE_BUCKETS)), None)
        bucket = deque()
        _RATE_BUCKETS[key] = bucket

    cutoff = now - window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.popleft()

    if len(bucket) >= max_calls:
        retry_after = max(1, int(window_seconds - (now - bucket[0])))
        raise HTTPException(
            429,
            detail="Too many requests — please slow down and try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )
    bucket.append(now)


def rate_limit(max_calls: int, window_seconds: float, *, scope: str) -> Callable:
    """FastAPI dependency factory: allow at most ``max_calls`` per client per
    ``window_seconds`` for routes sharing ``scope``.

        @router.post("/", dependencies=[Depends(rate_limit(20, 60, scope="leads"))])

    No-op under pytest: the functional suite hammers these routes from a single
    client and would trip the shared in-process buckets; the limiter logic is
    covered directly via _rate_limit_check in tests/test_auth_security.py.
    """
    async def _dep(request: Request) -> None:
        if "pytest" in sys.modules:
            return
        _rate_limit_check(f"{scope}:{_client_key(request)}", max_calls, window_seconds)

    return _dep
