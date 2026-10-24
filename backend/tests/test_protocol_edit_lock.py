"""Approved-protocol edit lock (B2, user-approved decision).

``PATCH /api/protocols/{id}/sections/{name}`` (AI regeneration) and
``PUT /api/protocols/{id}/sections/{name}`` (manual save) must refuse to change a
signed protocol: the e-signature binds to an exact document state (21 CFR
Part 11), so an edit after signing would silently invalidate it.

Real ProtocolStatus values are draft / review / approved. Only ``approved`` is
ever assigned by the code (POST /sign), so it is the one LOCKED status; ``draft``
and ``review`` stay editable (no route treats ``review`` as read-only).

Protocols are seeded straight into the DB with known section text, so no AI and
no HTTP mocks are needed except where a regeneration is expected to succeed
(``AIService.generate_protocol_section`` is replaced by an AsyncMock).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from models import AuditLog, Protocol, ProtocolStatus, ProtocolVersion, StudyType, User, UserRole
from routers import protocols as router
from security import hash_password

ORIGINAL = "<p>Original objectives text.</p>"


def _as(user: User) -> dict[str, str]:
    return {"X-User-Id": str(user.id)}


async def _seed(db, seeded, *, status: ProtocolStatus) -> uuid.UUID:
    protocol = Protocol(
        id=uuid.uuid4(),
        org_id=seeded.user.org_id,
        drug_id=seeded.drug.id,
        study_type=StudyType.be,
        population_config={"dose_strength": "850 mg"},
        sections={"objectives": ORIGINAL, "synopsis": "<p>Synopsis.</p>"},
        version=3,
        status=status,
        guideline_version="FDA-BE-2014",
        created_by=seeded.user.id,
    )
    db.add(protocol)
    await db.commit()
    return protocol.id


async def _version_count(db, protocol_id) -> int:
    result = await db.execute(
        select(func.count()).select_from(ProtocolVersion).where(
            ProtocolVersion.protocol_id == protocol_id
        )
    )
    return result.scalar_one()


async def _assert_untouched(db, protocol_id) -> None:
    db.expire_all()
    protocol = await db.get(Protocol, protocol_id)
    assert protocol.version == 3
    assert protocol.sections["objectives"] == ORIGINAL
    assert await _version_count(db, protocol_id) == 0


@pytest.fixture()
def ai_regen(monkeypatch) -> AsyncMock:
    """Replace the single-section AI call; tests assert whether it ran."""
    mock = AsyncMock(return_value="<p>Regenerated objectives.</p>")
    monkeypatch.setattr(router.AIService, "generate_protocol_section", mock)
    return mock


# ---------------------------------------------------------------------------
# Locked: approved
# ---------------------------------------------------------------------------


async def test_manual_save_on_approved_protocol_is_409(api_client, seeded):
    pid = await _seed(seeded.db, seeded, status=ProtocolStatus.approved)

    resp = await api_client.put(
        f"/api/protocols/{pid}/sections/objectives",
        json={"content": "<p>Sneaky post-signature edit.</p>"},
        headers=_as(seeded.user),
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "signed" in detail and "locked" in detail
    assert "approved" in detail
    assert "Clone" in detail  # tells the user how to proceed
    await _assert_untouched(seeded.db, pid)


async def test_ai_regeneration_on_approved_protocol_is_409_and_spends_no_credits(
    api_client, seeded, ai_regen
):
    pid = await _seed(seeded.db, seeded, status=ProtocolStatus.approved)

    resp = await api_client.patch(
        f"/api/protocols/{pid}/sections/objectives",
        json={},
        headers=_as(seeded.user),
    )

    assert resp.status_code == 409, resp.text
    assert "locked" in resp.json()["detail"]
    ai_regen.assert_not_awaited()  # rejected BEFORE the AI call
    await _assert_untouched(seeded.db, pid)


async def test_no_audit_row_for_a_rejected_edit(api_client, seeded):
    pid = await _seed(seeded.db, seeded, status=ProtocolStatus.approved)
    await api_client.put(
        f"/api/protocols/{pid}/sections/objectives",
        json={"content": "<p>x</p>"},
        headers=_as(seeded.user),
    )
    result = await seeded.db.execute(
        select(AuditLog).where(
            AuditLog.resource_id == str(pid), AuditLog.action.like("MANUAL_EDIT:%")
        )
    )
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Still editable: draft and review
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [ProtocolStatus.draft, ProtocolStatus.review])
async def test_manual_save_allowed_while_draft_or_in_review(api_client, seeded, status):
    pid = await _seed(seeded.db, seeded, status=status)

    resp = await api_client.put(
        f"/api/protocols/{pid}/sections/objectives",
        json={"content": "<p>Edited.</p>"},
        headers=_as(seeded.user),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 4
    assert resp.json()["content"] == "<p>Edited.</p>"


@pytest.mark.parametrize("status", [ProtocolStatus.draft, ProtocolStatus.review])
async def test_ai_regeneration_allowed_while_draft_or_in_review(api_client, seeded, ai_regen, status):
    pid = await _seed(seeded.db, seeded, status=status)

    resp = await api_client.patch(
        f"/api/protocols/{pid}/sections/objectives", json={}, headers=_as(seeded.user),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 4
    assert resp.json()["content"] == "<p>Regenerated objectives.</p>"
    ai_regen.assert_awaited_once()


# ---------------------------------------------------------------------------
# End to end: sign, then try to edit
# ---------------------------------------------------------------------------


async def test_edit_is_refused_after_a_real_signature(api_client, seeded, ai_regen):
    # Headers captured up front: expire_all() below would otherwise make reading
    # seeded.user.id lazy-load outside a greenlet.
    user_h, approver_h = _as(seeded.user), _as(seeded.approver)
    pid = await _seed(seeded.db, seeded, status=ProtocolStatus.draft)

    before = await api_client.put(
        f"/api/protocols/{pid}/sections/objectives",
        json={"content": "<p>Final text.</p>"},
        headers=user_h,
    )
    assert before.status_code == 200

    signed = await api_client.post(
        f"/api/protocols/{pid}/sign",
        json={
            "signatory_name": "Dr. Approver",
            "reason": "Approved for submission",
            "reauth_password": seeded.password,
        },
        headers=approver_h,
    )
    assert signed.status_code == 200, signed.text
    signed_version = signed.json()["version"]

    save = await api_client.put(
        f"/api/protocols/{pid}/sections/objectives",
        json={"content": "<p>Post-signature change.</p>"},
        headers=user_h,
    )
    regen = await api_client.patch(
        f"/api/protocols/{pid}/sections/objectives", json={}, headers=user_h,
    )
    assert save.status_code == 409
    assert regen.status_code == 409
    ai_regen.assert_not_awaited()

    seeded.db.expire_all()
    protocol = await seeded.db.get(Protocol, pid)
    assert protocol.status == ProtocolStatus.approved
    assert protocol.version == signed_version
    assert protocol.sections["objectives"] == "<p>Final text.</p>"

    # Reading and cloning a signed protocol are unaffected by the lock.
    got = await api_client.get(f"/api/protocols/{pid}", headers=user_h)
    assert got.status_code == 200
    cloned = await api_client.post(f"/api/protocols/{pid}/clone", headers=user_h)
    assert cloned.status_code == 201
    assert cloned.json()["status"] == "draft"
    edit_clone = await api_client.put(
        f"/api/protocols/{cloned.json()['id']}/sections/objectives",
        json={"content": "<p>Edit on the clone.</p>"},
        headers=user_h,
    )
    assert edit_clone.status_code == 200


# ---------------------------------------------------------------------------
# Ordering: the lock does not change earlier gates
# ---------------------------------------------------------------------------


async def test_role_and_org_and_section_checks_still_come_before_the_lock(api_client, seeded):
    pid = await _seed(seeded.db, seeded, status=ProtocolStatus.approved)

    # reviewer/approver role -> 403 (not 409)
    as_approver = await api_client.put(
        f"/api/protocols/{pid}/sections/objectives",
        json={"content": "<p>x</p>"},
        headers=_as(seeded.approver),
    )
    assert as_approver.status_code == 403

    # another org's generator -> 403 (not 409, and not a leak that it is signed)
    other = User(
        id=uuid.uuid4(), org_id="other-org", email="gen@other.example",
        role=UserRole.generator, hashed_password=hash_password("x"),
    )
    seeded.db.add(other)
    await seeded.db.commit()
    cross_org = await api_client.put(
        f"/api/protocols/{pid}/sections/objectives",
        json={"content": "<p>x</p>"},
        headers=_as(other),
    )
    assert cross_org.status_code == 403
    assert "locked" not in cross_org.text

    # unknown section -> 422 (unchanged)
    bad_section = await api_client.put(
        f"/api/protocols/{pid}/sections/not_a_section",
        json={"content": "<p>x</p>"},
        headers=_as(seeded.user),
    )
    assert bad_section.status_code == 422

    # unknown protocol -> 404 (unchanged)
    missing = await api_client.put(
        f"/api/protocols/{uuid.uuid4()}/sections/objectives",
        json={"content": "<p>x</p>"},
        headers=_as(seeded.user),
    )
    assert missing.status_code == 404


def test_only_approved_is_a_locked_status():
    assert router._LOCKED_PROTOCOL_STATUSES == frozenset({ProtocolStatus.approved})
    # every defined status is either locked or one we deliberately leave editable
    assert {s.value for s in ProtocolStatus} == {"draft", "review", "approved"}
