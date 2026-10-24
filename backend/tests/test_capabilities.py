"""GET /api/users/me/capabilities -> {role, is_org_owner, is_admin, can_generate}.

Driven through the REAL HTTP dependency chain (not a hand-built Request), the
same way tests/test_auth_security.py does: with Clerk auth OFF the caller is the
X-User-Id header; with Clerk auth patched ON the bearer token IS the caller's
email (auth._verify is stubbed), so the verified-session path -> ADMIN_EMAILS
gate is what is under test.
"""
from __future__ import annotations

import uuid

import pytest

from models import User, UserRole
from security import hash_password

pytestmark = pytest.mark.asyncio

_URL = "/api/users/me/capabilities"
_KEYS = {"role", "is_org_owner", "is_admin", "can_generate"}


def _bearer(email: str) -> dict:
    return {"Authorization": f"Bearer {email}"}


def _enable_clerk(monkeypatch):
    import auth

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "_verify", lambda token: {"sub": f"clerk_{token}", "email": token})


async def _add_user(db, email: str, role: UserRole, *, org_id: str = "test-org", owner: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        org_id=org_id,
        email=email,
        role=role,
        hashed_password=hash_password(uuid.uuid4().hex),
        is_org_owner=owner,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Role -> can_generate, owner flag (Clerk auth OFF: X-User-Id identity)
# ---------------------------------------------------------------------------


async def test_generator_can_generate(api_client, seeded):
    resp = await api_client.get(_URL, headers={"X-User-Id": str(seeded.user.id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == _KEYS
    assert body["role"] == "generator"
    assert body["can_generate"] is True
    assert body["is_org_owner"] is False


async def test_approver_cannot_generate(api_client, seeded):
    resp = await api_client.get(_URL, headers={"X-User-Id": str(seeded.approver.id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "approver"
    assert body["can_generate"] is False


async def test_reviewer_cannot_generate(api_client, seeded):
    reviewer = await _add_user(seeded.db, "reviewer@testplatform.com", UserRole.reviewer)
    resp = await api_client.get(_URL, headers={"X-User-Id": str(reviewer.id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "reviewer"
    assert body["can_generate"] is False


async def test_org_owner_flag_is_reported(api_client, seeded):
    owner = await _add_user(seeded.db, "owner@testplatform.com", UserRole.generator, owner=True)
    resp = await api_client.get(_URL, headers={"X-User-Id": str(owner.id)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_org_owner"] is True
    # ...and a plain member of the same org is not an owner
    member = await api_client.get(_URL, headers={"X-User-Id": str(seeded.user.id)})
    assert member.json()["is_org_owner"] is False


async def test_route_is_not_shadowed_by_the_users_routes(api_client, seeded):
    """/me/capabilities must return the capabilities shape, and /me must still
    return the user row (the two share the /me prefix)."""
    hdr = {"X-User-Id": str(seeded.user.id)}
    caps = await api_client.get(_URL, headers=hdr)
    me = await api_client.get("/api/users/me", headers=hdr)
    assert caps.status_code == 200 and set(caps.json()) == _KEYS
    assert me.status_code == 200 and me.json()["email"] == "generator@testplatform.com"
    assert "can_generate" not in me.json()


async def test_requires_an_identity(api_client, seeded):
    resp = await api_client.get(_URL)  # no X-User-Id
    assert resp.status_code == 401


async def test_is_admin_is_true_when_clerk_auth_is_off(api_client, seeded):
    """Matches dependencies.require_admin, which is a no-op with auth off
    (local dev / tests have no session to check an email against)."""
    resp = await api_client.get(_URL, headers={"X-User-Id": str(seeded.user.id)})
    assert resp.json()["is_admin"] is True


# ---------------------------------------------------------------------------
# is_admin via the verified session + ADMIN_EMAILS (Clerk auth ON)
# ---------------------------------------------------------------------------


async def test_admin_email_gets_is_admin_true(api_client, seeded, monkeypatch):
    _enable_clerk(monkeypatch)
    monkeypatch.setenv("ADMIN_EMAILS", "somebody@else.example, GENERATOR@testplatform.com")
    resp = await api_client.get(_URL, headers=_bearer("generator@testplatform.com"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_admin"] is True  # case-insensitive, whitespace-tolerant list
    assert body["role"] == "generator" and body["can_generate"] is True


async def test_non_admin_email_gets_is_admin_false(api_client, seeded, monkeypatch):
    _enable_clerk(monkeypatch)
    monkeypatch.setenv("ADMIN_EMAILS", "operator@protocolai.example")
    resp = await api_client.get(_URL, headers=_bearer("generator@testplatform.com"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_admin"] is False


async def test_no_admin_emails_configured_means_nobody_is_admin(api_client, seeded, monkeypatch):
    _enable_clerk(monkeypatch)
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    resp = await api_client.get(_URL, headers=_bearer("approver@testplatform.com"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_admin"] is False
    assert body["role"] == "approver" and body["can_generate"] is False


async def test_identity_comes_from_the_verified_session_not_x_user_id(api_client, seeded, monkeypatch):
    """With Clerk on, a spoofed X-User-Id naming the generator is ignored: the
    verified email (the approver) decides role and can_generate."""
    _enable_clerk(monkeypatch)
    resp = await api_client.get(
        _URL,
        headers={**_bearer("approver@testplatform.com"), "X-User-Id": str(seeded.user.id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "approver"
    assert resp.json()["can_generate"] is False


async def test_clerk_on_requires_a_session(api_client, seeded, monkeypatch):
    _enable_clerk(monkeypatch)
    assert (await api_client.get(_URL)).status_code == 401


async def test_clerk_on_unprovisioned_email_is_403(api_client, seeded, monkeypatch):
    _enable_clerk(monkeypatch)
    resp = await api_client.get(_URL, headers=_bearer("stranger@nowhere.example"))
    assert resp.status_code == 403
