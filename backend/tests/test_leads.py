"""Integration tests for the "Contact sales" lead-capture endpoints
(routers/leads.py) — public submission + admin-gated listing.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_create_lead_is_public_and_persists(api_client):
    """No auth header required — this is the landing page's Enterprise
    'Contact sales' form."""
    resp = await api_client.post(
        "/api/leads/",
        json={
            "company_name": "Acme Pharma",
            "contact_name": "Jane Doe",
            "contact_email": "Jane@Acme.example",
            "team_size_estimate": "50-100",
            "message": "Interested in an enterprise license for our BE team.",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["company_name"] == "Acme Pharma"
    assert body["contact_email"] == "jane@acme.example"  # lowercased
    assert body["status"] == "new"
    assert "id" in body


async def test_create_lead_requires_company_and_contact_fields(api_client):
    resp = await api_client.post("/api/leads/", json={"company_name": "Acme Pharma"})
    assert resp.status_code == 422


async def test_create_lead_allows_omitting_optional_fields(api_client):
    resp = await api_client.post(
        "/api/leads/",
        json={"company_name": "Beta CRO", "contact_name": "Sam Lee", "contact_email": "sam@beta.example"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["team_size_estimate"] is None
    assert body["message"] is None


async def test_list_leads_returns_newest_first(api_client):
    await api_client.post(
        "/api/leads/",
        json={"company_name": "First Co", "contact_name": "A", "contact_email": "a@first.example"},
    )
    await api_client.post(
        "/api/leads/",
        json={"company_name": "Second Co", "contact_name": "B", "contact_email": "b@second.example"},
    )

    resp = await api_client.get("/api/leads/")
    assert resp.status_code == 200
    companies = [row["company_name"] for row in resp.json()]
    assert companies[:2] == ["Second Co", "First Co"]


async def test_list_leads_rejects_non_admin_when_auth_enabled(monkeypatch):
    """With Clerk auth ON, GET /api/leads/ is gated by ADMIN_EMAILS — an
    ordinary verified session (not on the allowlist) gets 403."""
    import os
    from types import SimpleNamespace as NS

    import auth
    from dependencies import require_admin

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setenv("ADMIN_EMAILS", "founder@protocolai.example")

    async def _fake_email_for_sub(sub):
        return "not-the-founder@random.example"

    monkeypatch.setattr(auth, "clerk_email_for_sub", _fake_email_for_sub)

    req = NS(state=NS(clerk_claims={"sub": "clerk_user_1"}))
    with pytest.raises(Exception) as exc_info:
        await require_admin(req)
    assert getattr(exc_info.value, "status_code", None) == 403


async def test_list_leads_allows_admin_when_auth_enabled(monkeypatch):
    """The allowlisted operator email passes require_admin."""
    from types import SimpleNamespace as NS

    import auth
    from dependencies import require_admin

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setenv("ADMIN_EMAILS", "founder@protocolai.example")

    async def _fake_email_for_sub(sub):
        return "founder@protocolai.example"

    monkeypatch.setattr(auth, "clerk_email_for_sub", _fake_email_for_sub)

    req = NS(state=NS(clerk_claims={"sub": "clerk_user_1"}))
    result = await require_admin(req)
    assert result == "founder@protocolai.example"


async def test_get_leads_http_end_to_end_admin_gate(api_client, monkeypatch):
    """Drives the REAL route through the actual HTTP/dependency chain
    (Authorization header -> auth.require_user -> request.state.clerk_claims
    -> require_admin) rather than unit-testing require_admin in isolation
    with a hand-built fake Request. This is the test that would have caught
    a real bug found live: GET /api/leads/ was missing
    dependencies=[Depends(auth.require_user)], so clerk_claims was NEVER
    set and every caller — admin or not — got 401 "Authentication
    required", regardless of ADMIN_EMAILS. See
    [[project_launch_readiness_g4]]."""
    import auth

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setenv("ADMIN_EMAILS", "founder@protocolai.example")
    monkeypatch.setattr(auth, "_verify", lambda token: {"sub": "clerk_user_1", "email": token})

    admin_resp = await api_client.get(
        "/api/leads/", headers={"Authorization": "Bearer founder@protocolai.example"},
    )
    assert admin_resp.status_code == 200, admin_resp.text

    non_admin_resp = await api_client.get(
        "/api/leads/", headers={"Authorization": "Bearer someone-else@random.example"},
    )
    assert non_admin_resp.status_code == 403, non_admin_resp.text

    no_auth_resp = await api_client.get("/api/leads/")
    assert no_auth_resp.status_code == 401, no_auth_resp.text


async def test_delete_lead_removes_it_and_requires_admin(api_client, monkeypatch):
    import auth

    create_resp = await api_client.post(
        "/api/leads/",
        json={"company_name": "Delete Me Co", "contact_name": "X", "contact_email": "x@delete.example"},
    )
    lead_id = create_resp.json()["id"]

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setenv("ADMIN_EMAILS", "founder@protocolai.example")
    monkeypatch.setattr(auth, "_verify", lambda token: {"sub": "clerk_user_1", "email": token})

    non_admin_resp = await api_client.delete(
        f"/api/leads/{lead_id}", headers={"Authorization": "Bearer someone-else@random.example"},
    )
    assert non_admin_resp.status_code == 403

    admin_resp = await api_client.delete(
        f"/api/leads/{lead_id}", headers={"Authorization": "Bearer founder@protocolai.example"},
    )
    assert admin_resp.status_code == 204

    listing = await api_client.get(
        "/api/leads/", headers={"Authorization": "Bearer founder@protocolai.example"},
    )
    assert lead_id not in [row["id"] for row in listing.json()]


async def test_delete_lead_404_for_unknown_id(api_client, monkeypatch):
    import auth

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setenv("ADMIN_EMAILS", "founder@protocolai.example")
    monkeypatch.setattr(auth, "_verify", lambda token: {"sub": "clerk_user_1", "email": token})

    resp = await api_client.delete(
        "/api/leads/00000000-0000-0000-0000-000000000000",
        headers={"Authorization": "Bearer founder@protocolai.example"},
    )
    assert resp.status_code == 404
