"""Integration tests for persistent section review comments
(routers/comments.py) — CRUD plus the org-isolation boundary.

Auth is OFF in this suite (conftest clears CLERK_ISSUER), so the acting user is
supplied via the X-User-Id header, which is exactly the identity path
authenticate_actor falls back to in that mode.
"""
from __future__ import annotations

import uuid

import pytest
from models import Comment, Protocol, ProtocolStatus, StudyType, User, UserRole
from security import hash_password

pytestmark = pytest.mark.asyncio


async def _make_protocol(db, drug, creator, org_id: str) -> Protocol:
    """Insert a protocol row directly — these tests are about the comment
    routes, not the generation pipeline (which needs AI/PubMed mocks)."""
    protocol = Protocol(
        id=uuid.uuid4(),
        org_id=org_id,
        drug_id=drug.id,
        study_type=StudyType.be,
        population_config={"population": "healthy_volunteers"},
        sections={"study_design": "Placeholder section text."},
        version=1,
        status=ProtocolStatus.draft,
        guideline_version="FDA-BE-2014",
        created_by=creator.id,
    )
    db.add(protocol)
    await db.commit()
    await db.refresh(protocol)
    return protocol


def _as(user: User) -> dict[str, str]:
    return {"X-User-Id": str(user.id)}


async def test_create_comment_then_read_it_back(api_client, seeded):
    protocol = await _make_protocol(seeded.db, seeded.drug, seeded.user, seeded.user.org_id)

    create = await api_client.post(
        f"/api/protocols/{protocol.id}/comments",
        json={"section_name": "study_design", "text": "Please justify the washout period."},
        headers=_as(seeded.user),
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["section_name"] == "study_design"
    assert body["text"] == "Please justify the washout period."
    assert body["resolved"] is False
    # Attribution comes from the resolved actor, never the client.
    assert body["author_name"] == seeded.user.email

    listing = await api_client.get(
        f"/api/protocols/{protocol.id}/comments", headers=_as(seeded.user),
    )
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert [r["id"] for r in rows] == [body["id"]]


async def test_comments_are_visible_to_other_members_of_the_same_org(api_client, seeded):
    """The whole point of moving off localStorage: a note left by the author is
    readable by the org's approver."""
    protocol = await _make_protocol(seeded.db, seeded.drug, seeded.user, seeded.user.org_id)

    await api_client.post(
        f"/api/protocols/{protocol.id}/comments",
        json={"section_name": "statistical_analysis", "text": "Check the CV% source."},
        headers=_as(seeded.user),
    )

    listing = await api_client.get(
        f"/api/protocols/{protocol.id}/comments", headers=_as(seeded.approver),
    )
    assert listing.status_code == 200, listing.text
    assert [r["text"] for r in listing.json()] == ["Check the CV% source."]


async def test_patch_sets_resolved(api_client, seeded):
    protocol = await _make_protocol(seeded.db, seeded.drug, seeded.user, seeded.user.org_id)
    created = await api_client.post(
        f"/api/protocols/{protocol.id}/comments",
        json={"section_name": "study_design", "text": "Typo in section 4."},
        headers=_as(seeded.user),
    )
    comment_id = created.json()["id"]

    resolved = await api_client.patch(
        f"/api/protocols/{protocol.id}/comments/{comment_id}",
        json={"resolved": True},
        headers=_as(seeded.approver),
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolved"] is True

    reopened = await api_client.patch(
        f"/api/protocols/{protocol.id}/comments/{comment_id}",
        json={"resolved": False},
        headers=_as(seeded.user),
    )
    assert reopened.status_code == 200
    assert reopened.json()["resolved"] is False


async def test_delete_returns_204_and_removes_the_comment(api_client, seeded):
    protocol = await _make_protocol(seeded.db, seeded.drug, seeded.user, seeded.user.org_id)
    created = await api_client.post(
        f"/api/protocols/{protocol.id}/comments",
        json={"section_name": "study_design", "text": "Delete me."},
        headers=_as(seeded.user),
    )
    comment_id = created.json()["id"]

    deleted = await api_client.delete(
        f"/api/protocols/{protocol.id}/comments/{comment_id}", headers=_as(seeded.user),
    )
    assert deleted.status_code == 204, deleted.text

    listing = await api_client.get(
        f"/api/protocols/{protocol.id}/comments", headers=_as(seeded.user),
    )
    assert listing.json() == []

    assert await seeded.db.get(Comment, uuid.UUID(comment_id)) is None


async def test_delete_404_for_unknown_comment(api_client, seeded):
    protocol = await _make_protocol(seeded.db, seeded.drug, seeded.user, seeded.user.org_id)
    resp = await api_client.delete(
        f"/api/protocols/{protocol.id}/comments/00000000-0000-0000-0000-000000000000",
        headers=_as(seeded.user),
    )
    assert resp.status_code == 404, resp.text


async def test_cross_org_access_is_forbidden(api_client, seeded):
    """A user from another organization can neither read nor write notes on
    this protocol — get_protocol_scoped 403s on every comment route."""
    outsider = User(
        id=uuid.uuid4(),
        org_id="other-org",
        email="outsider@other.example",
        role=UserRole.generator,
        hashed_password=hash_password("irrelevant"),
    )
    seeded.db.add(outsider)
    await seeded.db.commit()
    await seeded.db.refresh(outsider)

    protocol = await _make_protocol(seeded.db, seeded.drug, seeded.user, seeded.user.org_id)
    created = await api_client.post(
        f"/api/protocols/{protocol.id}/comments",
        json={"section_name": "study_design", "text": "Internal note."},
        headers=_as(seeded.user),
    )
    comment_id = created.json()["id"]

    assert (
        await api_client.get(
            f"/api/protocols/{protocol.id}/comments", headers=_as(outsider),
        )
    ).status_code == 403

    assert (
        await api_client.post(
            f"/api/protocols/{protocol.id}/comments",
            json={"section_name": "study_design", "text": "I should not be here."},
            headers=_as(outsider),
        )
    ).status_code == 403

    assert (
        await api_client.patch(
            f"/api/protocols/{protocol.id}/comments/{comment_id}",
            json={"resolved": True},
            headers=_as(outsider),
        )
    ).status_code == 403

    assert (
        await api_client.delete(
            f"/api/protocols/{protocol.id}/comments/{comment_id}", headers=_as(outsider),
        )
    ).status_code == 403
