"""
Clerk session-token verification for the API.

The frontend attaches the Clerk session JWT as ``Authorization: Bearer <token>``.
This module verifies it (RS256 against Clerk's published JWKS) and exposes a
FastAPI dependency, ``require_user``, that rejects unauthenticated requests
with HTTP 401.

Auth is ENABLED only when ``CLERK_ISSUER`` is set. If it is empty/unset the
dependency lets every request through — this keeps local/offline testing and
the no-key fallback working, mirroring the frontend's ``authEnabled`` flag.
Unset CLERK_ISSUER (or comment it out in .env) to run the API fully open.
"""
import os
import time

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from jwt import PyJWKClient

load_dotenv()

CLERK_ISSUER = os.getenv("CLERK_ISSUER", "").rstrip("/")
CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "")
AUTH_ENABLED = bool(CLERK_ISSUER)

# PyJWKClient fetches and caches Clerk's rotating signing keys.
_jwks_client: PyJWKClient | None = (
    PyJWKClient(f"{CLERK_ISSUER}/.well-known/jwks.json") if AUTH_ENABLED else None
)

# sub -> (email, expiry_epoch) cache for the Clerk Backend API lookup below.
# Clerk session tokens don't carry the email by default, so we resolve it once
# per user and cache it briefly (the app maps that email to a users-table row).
_CLERK_EMAIL_TTL = 300.0  # seconds
_clerk_email_cache: dict[str, tuple[str, float]] = {}


async def clerk_email_for_sub(sub: str) -> str | None:
    """Resolve a Clerk user id (JWT ``sub``) to its primary email address via the
    Clerk Backend API, cached for _CLERK_EMAIL_TTL. Returns None if it can't be
    resolved (no secret key, network error, unknown user, no primary email).

    This is the binding between a verified Clerk identity and the local users
    table: dependencies.authenticate_actor looks up ``User WHERE email == this``
    so the acting identity comes from the verified session, never from the
    client-supplied X-User-Id header.
    """
    if not sub or not CLERK_SECRET_KEY:
        return None
    now = time.time()
    cached = _clerk_email_cache.get(sub)
    if cached and cached[1] > now:
        return cached[0]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"https://api.clerk.com/v1/users/{sub}",
                headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}"},
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        primary_id = data.get("primary_email_address_id")
        email = None
        for addr in data.get("email_addresses", []):
            if addr.get("id") == primary_id:
                email = addr.get("email_address")
                break
        if not email:
            addrs = data.get("email_addresses", [])
            email = addrs[0].get("email_address") if addrs else None
        if email:
            _clerk_email_cache[sub] = (email, now + _CLERK_EMAIL_TTL)
        return email
    except Exception:  # noqa: BLE001 — any lookup failure ⇒ unresolved (caller 401/403s)
        return None


def _verify(token: str) -> dict:
    """Blocking JWKS fetch + signature/claims verification (run off-loop)."""
    assert _jwks_client is not None
    signing_key = _jwks_client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=CLERK_ISSUER,
        options={"verify_aud": False},  # Clerk session tokens carry no fixed aud
        leeway=10,                       # small clock-skew tolerance
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_user(request: Request) -> str | None:
    """FastAPI dependency: returns the Clerk user id (``sub``) or raises 401.

    Returns ``None`` (allows the request) when auth is disabled, so the API
    remains usable without Clerk configured.
    """
    if not AUTH_ENABLED:
        return None

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise _unauthorized("Authentication required.")

    token = header[7:].strip()
    if not token:
        raise _unauthorized("Authentication required.")

    try:
        claims = await run_in_threadpool(_verify, token)
    except Exception as exc:  # noqa: BLE001 — any verification failure ⇒ 401
        raise _unauthorized("Invalid or expired session.") from exc

    # Stash the verified claims so dependencies.authenticate_actor can derive the
    # acting user from THIS (the real, signed session) rather than the spoofable
    # X-User-Id header. require_user is a dependency on every route that then
    # calls authenticate_actor, so it always runs first.
    request.state.clerk_claims = claims
    return claims.get("sub")
