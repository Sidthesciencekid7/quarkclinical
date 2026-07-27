"""Cross-cutting authentication / authorization boundary tests.

These deliberately drive the REAL HTTP routes through the full FastAPI
dependency chain with Clerk auth patched ON (Authorization: Bearer <email>,
verified via a stubbed auth._verify), rather than unit-testing the auth
helpers in isolation with hand-built fake Requests. That isolation gap is
exactly what let two real production bugs through earlier:

  1. GET /api/leads/ was missing dependencies=[Depends(auth.require_user)],
     so request.state.clerk_claims was never set and every caller got 401.
  2. GET /api/users/ returned every user across ALL orgs to any signed-in
     session (cross-tenant PII leak), and POST /api/pipeline/generate let any
     signup drain Anthropic credits.

The stub: auth._verify(token) -> {"sub": ..., "email": token}. So the bearer
token IS the caller's email — pass "owner@org.example" to act as that user.
Signature verification and the Clerk Backend API lookup are bypassed; the
authorization logic (provisioned? which org? admin? active license?) is what's
under test.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from models import AuditLog, User

pytestmark = pytest.mark.asyncio


def _bearer(email: str) -> dict:
    return {"Authorization": f"Bearer {email}"}


def _enable_clerk(monkeypatch):
    import auth

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "_verify", lambda token: {"sub": f"clerk_{token}", "email": token})


# ---------------------------------------------------------------------------
# GET /api/users/ — cross-org PII isolation (the leak fix)
# ---------------------------------------------------------------------------


async def test_list_users_scoped_to_own_org_and_ignores_client_org_id(api_client, monkeypatch):
    """Signed-in as org A's owner, GET /api/users/ returns ONLY org A's users
    — never org B's — and a client-supplied ?org_id naming org B is ignored."""
    # Two separate orgs, each with its own owner (created with auth OFF).
    await api_client.post("/api/orgs/", json={"name": "Org A", "email": "owner-a@a.example"})
    org_b = await api_client.post("/api/orgs/", json={"name": "Org B", "email": "owner-b@b.example"})
    org_b_slug = org_b.json()["slug"]

    _enable_clerk(monkeypatch)

    resp = await api_client.get("/api/users/", headers=_bearer("owner-a@a.example"))
    assert resp.status_code == 200, resp.text
    emails = {u["email"] for u in resp.json()}
    assert emails == {"owner-a@a.example"}
    assert "owner-b@b.example" not in emails

    # Attempt to pivot to org B via the query param — must stay scoped to caller.
    spoof = await api_client.get(
        f"/api/users/?org_id={org_b_slug}", headers=_bearer("owner-a@a.example"),
    )
    assert spoof.status_code == 200
    assert {u["email"] for u in spoof.json()} == {"owner-a@a.example"}


async def test_list_users_rejects_unprovisioned_session(api_client, monkeypatch):
    """A valid Clerk session whose email isn't provisioned in any org gets 403
    — it can no longer enumerate every customer's users. (Public sign-up means
    'any valid session' = anyone on the internet, so this must not leak.)"""
    await api_client.post("/api/orgs/", json={"name": "Real Org", "email": "real@org.example"})

    _enable_clerk(monkeypatch)

    resp = await api_client.get("/api/users/", headers=_bearer("stranger@nowhere.example"))
    assert resp.status_code == 403, resp.text


async def test_list_users_requires_a_session_when_auth_enabled(api_client, monkeypatch):
    """No Authorization header at all → 401, not an open listing."""
    _enable_clerk(monkeypatch)
    resp = await api_client.get("/api/users/")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# POST /api/pipeline/generate — credit-drain gate (provisioned + active license)
# ---------------------------------------------------------------------------


async def test_pipeline_generate_rejects_unprovisioned_session(api_client, monkeypatch):
    """An unprovisioned Clerk signup cannot reach the credit-spending pipeline
    — authenticate_actor 403s before run_pipeline is ever called."""
    _enable_clerk(monkeypatch)
    resp = await api_client.post(
        "/api/pipeline/generate",
        json={"query": "metformin", "user_params": {}},
        headers=_bearer("stranger@nowhere.example"),
    )
    assert resp.status_code == 403, resp.text


async def test_pipeline_generate_blocked_by_pending_license(api_client, monkeypatch):
    """A provisioned owner whose org license is still pending gets 402 before
    any AI call — same license gate as POST /api/protocols."""
    await api_client.post("/api/orgs/", json={"name": "Pipe Org", "email": "owner@pipe.example"})

    _enable_clerk(monkeypatch)
    resp = await api_client.post(
        "/api/pipeline/generate",
        json={"query": "metformin", "user_params": {}},
        headers=_bearer("owner@pipe.example"),
    )
    assert resp.status_code == 402, resp.text


async def test_pipeline_generate_requires_a_session(api_client, monkeypatch):
    _enable_clerk(monkeypatch)
    resp = await api_client.post(
        "/api/pipeline/generate", json={"query": "metformin", "user_params": {}},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Admin routes — regression guard that the ADMIN_EMAILS gate holds end-to-end
# (would have caught the leads-GET missing-dependency bug for the org routes too)
# ---------------------------------------------------------------------------


async def test_admin_org_routes_reject_non_admin_end_to_end(api_client, monkeypatch):
    """A provisioned, non-admin session is 403'd on every admin org route,
    driven through the real HTTP dependency chain."""
    create = await api_client.post(
        "/api/orgs/", json={"name": "Non Admin Org", "email": "owner@nonadmin.example"},
    )
    org_id = create.json()["id"]

    _enable_clerk(monkeypatch)
    monkeypatch.setenv("ADMIN_EMAILS", "operator@protocolai.example")

    hdr = _bearer("owner@nonadmin.example")  # provisioned, but NOT an admin
    assert (await api_client.get("/api/orgs/admin/list", headers=hdr)).status_code == 403
    assert (await api_client.post(f"/api/orgs/{org_id}/activate-license", json={}, headers=hdr)).status_code == 403
    assert (await api_client.post(f"/api/orgs/{org_id}/suspend-license", headers=hdr)).status_code == 403
    assert (await api_client.get("/api/leads/", headers=hdr)).status_code == 403


async def test_admin_org_routes_allow_admin_end_to_end(api_client, monkeypatch):
    """The ADMIN_EMAILS operator passes the same routes."""
    create = await api_client.post(
        "/api/orgs/", json={"name": "Admin View Org", "email": "owner@adminview.example"},
    )
    org_id = create.json()["id"]

    _enable_clerk(monkeypatch)
    monkeypatch.setenv("ADMIN_EMAILS", "operator@protocolai.example")

    hdr = _bearer("operator@protocolai.example")
    assert (await api_client.get("/api/orgs/admin/list", headers=hdr)).status_code == 200
    assert (await api_client.get("/api/leads/", headers=hdr)).status_code == 200
    assert (await api_client.post(f"/api/orgs/{org_id}/activate-license", json={}, headers=hdr)).status_code == 200


# ---------------------------------------------------------------------------
# Audit-trail integrity: attribution uses the VERIFIED actor, not X-User-Id
# ---------------------------------------------------------------------------


async def test_audit_trail_attributes_to_verified_actor_not_spoofed_header(
    api_client, patched_db, monkeypatch
):
    """A mutating request carrying a spoofed X-User-Id (a DIFFERENT real user's
    id) but a valid session for owner A must be recorded in the audit trail
    under owner A — the verified session — never the header. Otherwise an actor
    could falsify who the 21 CFR Part 11 trail says performed an action."""
    await api_client.post("/api/orgs/", json={"name": "Audit A", "email": "audit-a@a.example"})
    await api_client.post("/api/orgs/", json={"name": "Audit B", "email": "audit-b@b.example"})
    owner_a = (await patched_db.execute(
        select(User).where(User.email == "audit-a@a.example")
    )).scalar_one()
    owner_b = (await patched_db.execute(
        select(User).where(User.email == "audit-b@b.example")
    )).scalar_one()

    _enable_clerk(monkeypatch)
    # Verified session = owner A; spoofed X-User-Id = owner B's real UUID.
    resp = await api_client.post(
        "/api/orgs/rotate-join-token",
        headers={**_bearer("audit-a@a.example"), "X-User-Id": str(owner_b.id)},
    )
    assert resp.status_code == 200, resp.text
    await asyncio.sleep(0)  # let audit_logger's post-response write commit

    logs = (await patched_db.execute(
        select(AuditLog).where(AuditLog.action == "POST:organization")
    )).scalars().all()
    attributed = {log.user_id for log in logs}
    assert owner_a.id in attributed, "verified session actor was not attributed"
    assert owner_b.id not in attributed, "spoofed X-User-Id leaked into the audit trail"


# ---------------------------------------------------------------------------
# Audit-trail cross-org scoping for ORGANIZATION resource ids (not just protocols)
# ---------------------------------------------------------------------------


async def test_audit_trail_blocks_cross_org_organization_id(api_client, monkeypatch):
    """A non-admin cannot read another org's lifecycle audit trail by guessing
    its UUID — the org-scoping now covers organization ids, not only protocols."""
    org_a = await api_client.post("/api/orgs/", json={"name": "Scope A", "email": "scope-a@a.example"})
    org_b = await api_client.post("/api/orgs/", json={"name": "Scope B", "email": "scope-b@b.example"})

    _enable_clerk(monkeypatch)
    hdr = _bearer("scope-a@a.example")

    own = await api_client.get(f"/api/audit/{org_a.json()['id']}", headers=hdr)
    assert own.status_code == 200, own.text

    cross = await api_client.get(f"/api/audit/{org_b.json()['id']}", headers=hdr)
    assert cross.status_code == 403, cross.text


async def test_audit_trail_admin_bypasses_org_scoping(api_client, monkeypatch):
    """An ADMIN_EMAILS operator manages every org, so may read any org's trail."""
    org_b = await api_client.post("/api/orgs/", json={"name": "AdmScope B", "email": "asb@b.example"})
    await api_client.post("/api/orgs/", json={"name": "Ops", "email": "ops@protocolai.example"})

    _enable_clerk(monkeypatch)
    monkeypatch.setenv("ADMIN_EMAILS", "ops@protocolai.example")

    resp = await api_client.get(
        f"/api/audit/{org_b.json()['id']}", headers=_bearer("ops@protocolai.example"),
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Rate limiter (unit-tested directly — the dependency is a no-op under pytest)
# ---------------------------------------------------------------------------


def test_rate_limit_check_blocks_after_limit():
    from fastapi import HTTPException

    from dependencies import _RATE_BUCKETS, _rate_limit_check

    key = "unit_test_scope:203.0.113.7"
    _RATE_BUCKETS.pop(key, None)
    try:
        for _ in range(5):
            _rate_limit_check(key, max_calls=5, window_seconds=60)  # all within limit
        with pytest.raises(HTTPException) as exc:
            _rate_limit_check(key, max_calls=5, window_seconds=60)  # 6th trips
        assert exc.value.status_code == 429
        assert "Retry-After" in exc.value.headers
    finally:
        _RATE_BUCKETS.pop(key, None)


def test_rate_limit_check_is_per_key_independent():
    from dependencies import _RATE_BUCKETS, _rate_limit_check

    k1, k2 = "unit_scope:1.1.1.1", "unit_scope:2.2.2.2"
    _RATE_BUCKETS.pop(k1, None)
    _RATE_BUCKETS.pop(k2, None)
    try:
        _rate_limit_check(k1, max_calls=1, window_seconds=60)  # k1 uses its single slot
        # k2 has its own independent bucket — must NOT be affected by k1.
        _rate_limit_check(k2, max_calls=1, window_seconds=60)  # no raise
    finally:
        _RATE_BUCKETS.pop(k1, None)
        _RATE_BUCKETS.pop(k2, None)
