"""Integration tests for the organization / license / team-provisioning
endpoints (routers/orgs.py) — org creation, join-link redemption, seat limits,
owner-only gating, org-scoped member listing, and the manual (no-Stripe)
license-activation gate.

No payment integration exists — a self-serve org's License starts "pending"
and the operator manually flips it to "active" via
POST /{org_id}/activate-license once a sales-assisted deal is confirmed. A
non-active license blocks joining (existing behavior) AND blocks protocol
generation (dependencies.require_active_license, tested here and in
test_pipeline.py-style protocol-creation tests).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from models import License, LicenseStatus, Organization, User, UserRole

pytestmark = pytest.mark.asyncio


def _header(user_id) -> dict:
    return {"X-User-Id": str(user_id)}


async def _activate(api_client, org_id, **overrides) -> dict:
    """Test helper — exercises the real activation endpoint (admin gate is a
    no-op with Clerk auth disabled, matching every other org test's use of
    the bare X-User-Id/no-auth path)."""
    resp = await api_client.post(f"/api/orgs/{org_id}/activate-license", json=overrides)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_create_org_becomes_owner_with_pending_license(api_client):
    """A brand-new email creates its own org, becomes its owner, and gets a
    PENDING License with the default seat allowance — generation stays
    blocked until the operator manually activates it (no Stripe; see module
    docstring)."""
    resp = await api_client.post("/api/orgs/", json={"name": "Acme CRO", "email": "owner@acme.example"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Acme CRO"
    assert body["is_owner"] is True
    assert body["license"]["status"] == "pending"
    assert body["license"]["seats_used"] == 1
    assert body["license"]["seat_count"] == 20
    assert body["join_token"]  # owner sees the join link/token


async def test_create_org_rejects_already_provisioned_email(api_client, seeded):
    """An email that already has a users-table row can't spin up a second org."""
    resp = await api_client.post(
        "/api/orgs/", json={"name": "Second Org", "email": seeded.user.email},
    )
    assert resp.status_code == 409


async def test_create_org_requires_email_when_auth_disabled(api_client):
    resp = await api_client.post("/api/orgs/", json={"name": "No Email Org"})
    assert resp.status_code == 422


async def test_join_org_adds_seat_under_existing_license(api_client, patched_db):
    """A second person redeeming the org's join token becomes a (non-owner)
    seat on the SAME org/license the first person created."""
    create_resp = await api_client.post(
        "/api/orgs/", json={"name": "Beta Labs", "email": "founder@beta.example"},
    )
    await _activate(api_client, create_resp.json()["id"])  # joining requires an active license
    token = create_resp.json()["join_token"]

    join_resp = await api_client.post(
        "/api/orgs/join", json={"token": token, "email": "teammate@beta.example"},
    )
    assert join_resp.status_code == 201, join_resp.text
    body = join_resp.json()
    assert body["is_owner"] is False
    assert body["license"]["seats_used"] == 2
    assert body["join_token"] is None  # non-owners never see the token

    result = await patched_db.execute(select(User).where(User.email == "teammate@beta.example"))
    member = result.scalar_one()
    assert member.org_id == create_resp.json()["slug"]
    assert member.is_org_owner is False


async def test_join_org_rejects_invalid_token(api_client):
    resp = await api_client.post(
        "/api/orgs/join", json={"token": "not-a-real-token", "email": "someone@nowhere.example"},
    )
    assert resp.status_code == 404


async def test_join_org_rejects_already_provisioned_email(api_client, seeded):
    create_resp = await api_client.post(
        "/api/orgs/", json={"name": "Gamma Inc", "email": "founder2@gamma.example"},
    )
    token = create_resp.json()["join_token"]
    resp = await api_client.post(
        "/api/orgs/join", json={"token": token, "email": seeded.user.email},
    )
    assert resp.status_code == 409


async def test_join_org_rejects_when_seats_full(api_client, patched_db):
    """A license with seat_count == current headcount blocks further joins."""
    create_resp = await api_client.post(
        "/api/orgs/", json={"name": "Tiny Org", "email": "solo@tiny.example"},
    )
    slug = create_resp.json()["slug"]
    token = create_resp.json()["join_token"]

    result = await patched_db.execute(select(Organization).where(Organization.slug == slug))
    org = result.scalar_one()
    result = await patched_db.execute(select(License).where(License.organization_id == org.id))
    lic = result.scalar_one()
    lic.seat_count = 1  # already at capacity (the owner is the only seat)
    lic.status = LicenseStatus.active  # must be active to isolate the seats-full path
    await patched_db.commit()

    resp = await api_client.post(
        "/api/orgs/join", json={"token": token, "email": "latecomer@tiny.example"},
    )
    assert resp.status_code == 403
    assert "seats" in resp.text.lower()


async def test_get_my_org_hides_join_token_from_non_owner(api_client, patched_db):
    create_resp = await api_client.post(
        "/api/orgs/", json={"name": "Delta Co", "email": "owner@delta.example"},
    )
    await _activate(api_client, create_resp.json()["id"])
    token = create_resp.json()["join_token"]
    join_resp = await api_client.post(
        "/api/orgs/join", json={"token": token, "email": "member@delta.example"},
    )
    member_id = join_resp.json()  # id is org id, need the actual user id
    result = await patched_db.execute(select(User).where(User.email == "member@delta.example"))
    member = result.scalar_one()

    result = await patched_db.execute(select(User).where(User.email == "owner@delta.example"))
    owner = result.scalar_one()

    owner_me = await api_client.get("/api/orgs/me", headers=_header(owner.id))
    assert owner_me.json()["join_token"] == token

    member_me = await api_client.get("/api/orgs/me", headers=_header(member.id))
    assert member_me.json()["join_token"] is None
    assert member_me.json()["is_owner"] is False


async def test_rotate_join_token_owner_only(api_client, patched_db):
    create_resp = await api_client.post(
        "/api/orgs/", json={"name": "Epsilon LLC", "email": "owner@epsilon.example"},
    )
    await _activate(api_client, create_resp.json()["id"])
    old_token = create_resp.json()["join_token"]
    join_resp = await api_client.post(
        "/api/orgs/join", json={"token": old_token, "email": "member@epsilon.example"},
    )

    result = await patched_db.execute(select(User).where(User.email == "owner@epsilon.example"))
    owner = result.scalar_one()
    result = await patched_db.execute(select(User).where(User.email == "member@epsilon.example"))
    member = result.scalar_one()

    # A non-owner may not rotate the token.
    forbidden = await api_client.post("/api/orgs/rotate-join-token", headers=_header(member.id))
    assert forbidden.status_code == 403

    rotated = await api_client.post("/api/orgs/rotate-join-token", headers=_header(owner.id))
    assert rotated.status_code == 200
    new_token = rotated.json()["join_token"]
    assert new_token != old_token

    # The old link no longer works; a third person must use the new one.
    stale = await api_client.post(
        "/api/orgs/join", json={"token": old_token, "email": "toolate@epsilon.example"},
    )
    assert stale.status_code == 404

    fresh = await api_client.post(
        "/api/orgs/join", json={"token": new_token, "email": "onTime@epsilon.example"},
    )
    assert fresh.status_code == 201


async def test_members_list_is_scoped_to_own_org_only(api_client, patched_db):
    """Two separate orgs' rosters never leak into each other, regardless of
    which org's members endpoint is called by whom."""
    org_a = await api_client.post("/api/orgs/", json={"name": "Org A", "email": "a-owner@a.example"})
    org_b = await api_client.post("/api/orgs/", json={"name": "Org B", "email": "b-owner@b.example"})

    result = await patched_db.execute(select(User).where(User.email == "a-owner@a.example"))
    a_owner = result.scalar_one()
    result = await patched_db.execute(select(User).where(User.email == "b-owner@b.example"))
    b_owner = result.scalar_one()

    a_members = await api_client.get("/api/orgs/members", headers=_header(a_owner.id))
    assert a_members.status_code == 200
    a_emails = {m["email"] for m in a_members.json()}
    assert a_emails == {"a-owner@a.example"}

    b_members = await api_client.get("/api/orgs/members", headers=_header(b_owner.id))
    b_emails = {m["email"] for m in b_members.json()}
    assert b_emails == {"b-owner@b.example"}


async def test_org_endpoints_reject_clerk_auth_email_impersonation(seeded, monkeypatch):
    """With Clerk auth ON, a brand-new org's owner email comes from the
    VERIFIED session — the request body's email field is ignored entirely,
    mirroring authenticate_actor's existing spoof-proofing."""
    import auth
    from routers.orgs import _resolve_new_user_email
    from types import SimpleNamespace as NS

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)

    async def _fake_email_for_sub(sub):
        return "verified@real.example"

    monkeypatch.setattr(auth, "clerk_email_for_sub", _fake_email_for_sub)

    req = NS(state=NS(clerk_claims={"sub": "clerk_user_1"}))
    resolved = await _resolve_new_user_email(req, "attacker-supplied@spoofed.example")
    assert resolved == "verified@real.example"


# ---------------------------------------------------------------------------
# Manual (no-Stripe) license activation + the protocol-generation gate
# ---------------------------------------------------------------------------


async def test_activate_license_flips_status_and_allows_seat_override(api_client):
    """POST /{org_id}/activate-license is the whole "payment gate" — no
    webhook, the operator calls this once a deal is confirmed."""
    create_resp = await api_client.post(
        "/api/orgs/", json={"name": "Zeta Corp", "email": "owner@zeta.example"},
    )
    org_id = create_resp.json()["id"]

    activated = await _activate(api_client, org_id, seat_count=50, period_days=365)
    assert activated["license"]["status"] == "active"
    assert activated["license"]["seat_count"] == 50


async def test_activate_license_404_for_unknown_org(api_client):
    resp = await api_client.post(
        "/api/orgs/00000000-0000-0000-0000-000000000000/activate-license", json={},
    )
    assert resp.status_code == 404


async def test_suspend_license_flips_status_and_blocks_generation(api_client, seeded, patched_db):
    """The operator's kill switch — mirrors activate-license but the other
    direction. Suspending an already-active org blocks new protocol
    generation (dependencies.require_active_license) immediately."""
    from dependencies import require_active_license

    create_resp = await api_client.post(
        "/api/orgs/", json={"name": "Theta Two", "email": "owner@thetatwo.example"},
    )
    org_id = create_resp.json()["id"]
    await _activate(api_client, org_id)

    result = await patched_db.execute(select(User).where(User.email == "owner@thetatwo.example"))
    owner = result.scalar_one()
    patched_db.expunge_all()
    await require_active_license(patched_db, owner)  # active — no exception yet

    suspend_resp = await api_client.post(f"/api/orgs/{org_id}/suspend-license")
    assert suspend_resp.status_code == 200, suspend_resp.text
    assert suspend_resp.json()["license"]["status"] == "suspended"

    patched_db.expunge_all()
    with pytest.raises(Exception) as exc_info:
        await require_active_license(patched_db, owner)
    assert getattr(exc_info.value, "status_code", None) == 402


async def test_suspend_license_404_for_org_without_license(api_client):
    resp = await api_client.post(
        "/api/orgs/00000000-0000-0000-0000-000000000000/suspend-license",
    )
    assert resp.status_code == 404


async def test_admin_list_orgs_shows_days_remaining_only_when_active(api_client):
    """days_remaining reflects the real period_end once activated, and is
    None for a still-pending org (its period_end isn't a real deadline yet)."""
    create_resp = await api_client.post(
        "/api/orgs/", json={"name": "Iota Two", "email": "owner@iotatwo.example"},
    )
    org_id = create_resp.json()["id"]
    slug = create_resp.json()["slug"]

    pending_listing = await api_client.get("/api/orgs/admin/list")
    pending_row = next(row for row in pending_listing.json() if row["slug"] == slug)
    assert pending_row["days_remaining"] is None

    await _activate(api_client, org_id, period_days=365)

    active_listing = await api_client.get("/api/orgs/admin/list")
    active_row = next(row for row in active_listing.json() if row["slug"] == slug)
    assert active_row["days_remaining"] in (364, 365)  # allow for test-run timing


async def test_admin_list_orgs_shows_status_and_owner(api_client):
    """The operator's own dashboard-free view for finding who to activate."""
    resp = await api_client.post(
        "/api/orgs/", json={"name": "Eta Org", "email": "owner@eta.example"},
    )
    slug = resp.json()["slug"]

    listing = await api_client.get("/api/orgs/admin/list")
    assert listing.status_code == 200
    rows = {row["slug"]: row for row in listing.json()}
    assert slug in rows
    assert rows[slug]["owner_email"] == "owner@eta.example"
    assert rows[slug]["license_status"] == "pending"
    assert rows[slug]["seats_used"] == 1


async def test_protocol_creation_blocked_until_license_activated(api_client, seeded, patched_db):
    """The actual HTTP-level enforcement point: a self-serve org's owner
    cannot generate a protocol while its license is pending. No
    httpx_mock/mock_anthropic needed — dependencies.require_active_license
    runs before any literature fetch or AI call, same as the existing
    role-check test (test_create_protocol_requires_generator_role)."""
    create_resp = await api_client.post(
        "/api/orgs/", json={"name": "Theta Pharma", "email": "owner@theta.example"},
    )
    result = await patched_db.execute(select(User).where(User.email == "owner@theta.example"))
    owner = result.scalar_one()
    assert owner.role == UserRole.generator  # self-serve owners can author, just can't generate yet

    payload = {
        "drug_id": str(seeded.drug.id),
        "study_type": "BE",
        "population_config": {"dose_strength": "850 mg", "formulation": "tablet"},
        "guideline_version": "FDA-BE-2014",
    }
    blocked = await api_client.post(
        "/api/protocols/", json=payload, headers={"X-User-Id": str(owner.id)},
    )
    assert blocked.status_code == 402
    assert "license" in blocked.text.lower()


async def test_require_active_license_allows_once_activated(api_client, patched_db):
    """Unit-level check on the enforcement primitive itself — confirms
    activation actually clears the gate, without needing literature/AI mocks
    to drive a full HTTP generation."""
    from fastapi import HTTPException

    from dependencies import require_active_license

    create_resp = await api_client.post(
        "/api/orgs/", json={"name": "Iota Labs", "email": "owner@iota.example"},
    )
    org_id = create_resp.json()["id"]
    result = await patched_db.execute(select(User).where(User.email == "owner@iota.example"))
    owner = result.scalar_one()

    with pytest.raises(HTTPException) as exc_info:
        await require_active_license(patched_db, owner)
    assert exc_info.value.status_code == 402

    await _activate(api_client, org_id)
    # _activate committed through a *different* session (api_client's own,
    # via the overridden get_db dependency) — patched_db's identity map still
    # holds the pre-activation License object, so drop it and force a fresh
    # read (expunge_all does no IO itself, unlike expire_all, so it's safe
    # to call without an async greenlet bridge).
    patched_db.expunge_all()
    await require_active_license(patched_db, owner)  # no exception now


async def test_require_active_license_fails_open_for_orgs_without_organization_row(seeded):
    """Users predating the Organization/License model (or test fixtures like
    `seeded`, which create a User with org_id='test-org' directly, bypassing
    routers/orgs.py entirely) are NOT blocked — see the fail-open rationale
    in dependencies.require_active_license's docstring."""
    from dependencies import require_active_license

    await require_active_license(seeded.db, seeded.user)  # no exception


# ---------------------------------------------------------------------------
# ADMIN_EMAILS operators get "full access" — active license outright, and
# exempt from the license gate regardless of their org's stored status.
# ---------------------------------------------------------------------------


async def test_admin_self_serve_org_created_active(api_client, monkeypatch):
    """An ADMIN_EMAILS operator's own self-serve org skips "pending" entirely
    — admin access is meant to mean full product access, not just the admin
    endpoints, and the frontend has no separate way to know a viewer is an
    operator (see routers/orgs.py::create_org)."""
    monkeypatch.setenv("ADMIN_EMAILS", "owner@opsteam.example")

    resp = await api_client.post(
        "/api/orgs/", json={"name": "Ops Co", "email": "owner@opsteam.example"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["license"]["status"] == "active"


async def test_admin_exempt_from_license_gate_even_with_pending_org(api_client, patched_db, monkeypatch):
    """If an admin's org somehow still ended up pending (e.g. ADMIN_EMAILS
    was added after the org already existed), require_active_license exempts
    them anyway based on their own email — not the org's stored status."""
    from dependencies import require_active_license

    create_resp = await api_client.post(
        "/api/orgs/", json={"name": "Late Admin Org", "email": "lateadmin@opsteam.example"},
    )
    assert create_resp.json()["license"]["status"] == "pending"  # not admin yet at creation time

    result = await patched_db.execute(select(User).where(User.email == "lateadmin@opsteam.example"))
    owner = result.scalar_one()

    monkeypatch.setenv("ADMIN_EMAILS", "lateadmin@opsteam.example")
    await require_active_license(patched_db, owner)  # no exception despite the still-pending license
