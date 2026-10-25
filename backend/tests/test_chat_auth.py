"""Chat API gates (B4): who may do what.

Covers org isolation, the creator-only transcript rule (user decision
2026-10-23), the role gates (generator for protocol kinds; budgets open), the
license gates (a pending org can SAVE its landing prompt through POST /api/chat
but not run a turn or generate), the turn lock (409 busy), the stale-spec check
(409), the per-session / per-day caps (429), the 4,000-character limit, and the
identity binding when Clerk auth is on.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from models import License, LicenseStatus, Organization, Protocol, User, UserRole
from security import hash_password
from services.chat.store import SqlChatRepository
from tests.test_chat_api import (  # noqa: F401  (fixtures + helpers)
    active,
    as_user,
    click,
    create_chat,
    drive_to_summary,
    get_chat,
    names,
    offline,
    parse_sse,
    send_turn,
)

pytestmark = pytest.mark.asyncio


async def add_user(db, email: str, role: UserRole = UserRole.generator, *, org_id: str = "test-org") -> User:
    user = User(
        id=uuid.uuid4(), org_id=org_id, email=email, role=role, hashed_password=hash_password(uuid.uuid4().hex),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def detail_code(resp) -> str:
    return resp.json()["detail"]["code"]


# ---------------------------------------------------------------------------
# Org isolation and creator-only transcripts
# ---------------------------------------------------------------------------


async def test_another_org_gets_403_on_every_session_route(api_client, seeded):
    made = await create_chat(api_client, seeded.user, initial_message="hello")
    sid = made["id"]
    stranger = await add_user(seeded.db, "stranger@other.example", org_id="other-org")
    h = as_user(stranger)

    reads = [
        await api_client.get(f"/api/chat/{sid}", headers=h),
        await api_client.patch(f"/api/chat/{sid}", json={"title": "mine now"}, headers=h),
        await api_client.post(f"/api/chat/{sid}/turns", json={"text": "hi", "client_turn_id": "t1"}, headers=h),
        await api_client.post(
            f"/api/chat/{sid}/actions",
            json={"card_id": str(uuid.uuid4()), "action": "confirm_design", "payload": {}, "spec_version": 0},
            headers=h,
        ),
        await api_client.get(f"/api/chat/{sid}/proposals/{uuid.uuid4()}", headers=h),
    ]
    for resp in reads:
        assert resp.status_code == 403, resp.text
        assert detail_code(resp) == "forbidden"
    # The stranger's own list never shows it.
    assert (await api_client.get("/api/chat", headers=h)).json() == []
    # Nothing was changed by the rejected PATCH.
    assert (await get_chat(api_client, seeded.user, sid))["session"]["title"] == "hello"


async def test_an_unknown_session_is_404_and_a_malformed_id_is_422(api_client, seeded):
    resp = await api_client.get(f"/api/chat/{uuid.uuid4()}", headers=as_user(seeded.user))
    assert resp.status_code == 404 and detail_code(resp) == "not_found"
    assert (await api_client.get("/api/chat/not-a-uuid", headers=as_user(seeded.user))).status_code == 422


async def test_another_user_in_the_same_org_cannot_read_the_transcript(api_client, seeded):
    made = await create_chat(api_client, seeded.user, initial_message="my private study idea")
    sid = made["id"]
    colleague = await add_user(seeded.db, "colleague@testplatform.com")
    for who in (colleague, seeded.approver):
        h = as_user(who)
        for resp in (
            await api_client.get(f"/api/chat/{sid}", headers=h),
            await api_client.post(f"/api/chat/{sid}/turns", json={"resume": True, "client_turn_id": "x"}, headers=h),
            await api_client.patch(f"/api/chat/{sid}", json={"status": "archived"}, headers=h),
        ):
            assert resp.status_code == 403, resp.text
            assert detail_code(resp) == "forbidden"
        listed = await api_client.get("/api/chat", headers=h)
        assert listed.status_code == 200 and listed.json() == []
    assert [r["id"] for r in (await api_client.get("/api/chat", headers=as_user(seeded.user))).json()] == [sid]


async def test_a_protocol_edit_chat_cannot_target_another_orgs_protocol(api_client, seeded):
    protocol = Protocol(
        id=uuid.uuid4(), org_id="other-org", drug_id=seeded.drug.id, study_type="BE", population_config={},
        sections={"synopsis": "<p>x</p>"}, guideline_version="FDA-BE-2014", created_by=seeded.user.id,
    )
    seeded.db.add(protocol)
    await seeded.db.commit()
    resp = await api_client.post(
        "/api/chat", json={"kind": "protocol_edit", "protocol_id": str(protocol.id)}, headers=as_user(seeded.user),
    )
    assert resp.status_code == 403
    resp = await api_client.post(
        "/api/chat", json={"kind": "protocol_edit", "protocol_id": str(uuid.uuid4())}, headers=as_user(seeded.user),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


async def test_reviewer_and_approver_cannot_start_protocol_chats_but_can_start_budget_chats(api_client, seeded):
    reviewer = await add_user(seeded.db, "reviewer@testplatform.com", UserRole.reviewer)
    for who in (reviewer, seeded.approver):
        for kind, extra in (("protocol", {}), ("protocol_edit", {"protocol_id": str(uuid.uuid4())})):
            resp = await api_client.post("/api/chat", json={"kind": kind, **extra}, headers=as_user(who))
            assert resp.status_code == 403, (kind, resp.text)
            assert "not permitted" in resp.json()["detail"]
        ok = await api_client.post("/api/chat", json={"kind": "budget"}, headers=as_user(who))
        assert ok.status_code == 201, ok.text


async def test_a_role_change_after_creation_blocks_turns_and_actions(api_client, seeded):
    made = await create_chat(api_client, seeded.user, initial_message="hello")
    sid = made["id"]
    seeded.user.role = UserRole.reviewer
    await seeded.db.commit()
    turn = await send_turn(api_client, seeded.user, sid, resume=True)
    assert turn.status_code == 403
    resp = await click(api_client, seeded.user, sid, {"id": str(uuid.uuid4())}, "confirm_design", spec_version=0)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# License
# ---------------------------------------------------------------------------


async def _pending_org_owner(api_client, db, email="owner@pending.example"):
    created = await api_client.post("/api/orgs/", json={"name": "Pending Pharma", "email": email})
    assert created.status_code == 201, created.text
    owner = (await db.execute(select(User).where(User.email == email))).scalar_one()
    return created.json()["id"], owner


async def test_pending_license_can_save_a_prompt_but_not_run_turns(api_client, seeded):
    _org_id, owner = await _pending_org_owner(api_client, seeded.db)
    # POST /api/chat has no license check: the landing prompt is saved server-side.
    made = await create_chat(api_client, owner, initial_message="Bioequivalence study for metformin 850 mg tablets")
    sid = made["id"]
    detail = await get_chat(api_client, owner, sid)
    assert detail["awaiting_reply"] is True and detail["messages"][0]["text"].startswith("Bioequivalence")

    turn = await send_turn(api_client, owner, sid, resume=True)
    assert turn.status_code == 402
    assert "license" in turn.json()["detail"].lower()
    turn = await send_turn(api_client, owner, sid, "hello")
    assert turn.status_code == 402
    # Nothing ran: the stored message is still unanswered and the lock is free.
    detail = await get_chat(api_client, owner, sid)
    assert detail["awaiting_reply"] is True


async def test_generate_is_402_once_the_license_is_not_active(api_client, seeded, offline):
    """Drive a real draft to a ready summary on an active org, then lapse the
    license: the Generate click is refused (402) and no protocol is created."""
    org_id, owner = await _pending_org_owner(api_client, seeded.db, "owner@lapsing.example")
    activated = await api_client.post(f"/api/orgs/{org_id}/activate-license", json={})
    assert activated.status_code == 200, activated.text

    made = await create_chat(api_client, owner)
    sid = made["id"]
    turn = await send_turn(api_client, owner, sid, "A bioequivalence study of metformin")
    assert names(parse_sse(turn.text))[-1] == "turn_end"
    matches = active((await get_chat(api_client, owner, sid))["cards"], "drug_matches")
    await click(api_client, owner, sid, matches, "select_drug", {"drug_id": matches["data"]["matches"][0]["id"]})
    summary = await drive_to_summary(api_client, owner, sid)

    org = (await seeded.db.execute(select(Organization).where(Organization.id == uuid.UUID(org_id)))).scalar_one()
    lic = (await seeded.db.execute(select(License).where(License.organization_id == org.id))).scalars().first()
    lic.status = LicenseStatus.pending
    await seeded.db.commit()

    resp = await click(api_client, owner, sid, summary, "generate_protocol")
    assert resp.status_code == 402, resp.text
    assert (await seeded.db.execute(select(Protocol))).scalars().all() == []
    # The same click on an unrelated card id is refused up front too (no work done).
    assert (await get_chat(api_client, owner, sid))["spec"]["ready"] is True

    # Turns are refused as well; plain (credit-free) card clicks still work.
    assert (await send_turn(api_client, owner, sid, "show me the summary")).status_code == 402


# ---------------------------------------------------------------------------
# Lock, stale spec, caps, limits
# ---------------------------------------------------------------------------


async def test_a_busy_session_is_409_for_turns_and_actions(api_client, seeded, offline):
    made = await create_chat(api_client, seeded.user)
    sid = made["id"]
    repo = SqlChatRepository()
    assert await repo.acquire_turn_lock(sid, "someone-else")

    turn = await send_turn(api_client, seeded.user, sid, "A bioequivalence study of metformin")
    assert turn.status_code == 409 and detail_code(turn) == "busy"
    resp = await click(api_client, seeded.user, sid, {"id": str(uuid.uuid4())}, "confirm_design", spec_version=0)
    assert resp.status_code == 409 and detail_code(resp) == "busy"

    await repo.release_turn_lock(sid, "someone-else")
    ok = await send_turn(api_client, seeded.user, sid, "A bioequivalence study of metformin")
    assert ok.status_code == 200 and names(parse_sse(ok.text))[-1] == "turn_end"
    # Every request released its own lock again.
    assert not await repo.is_turn_locked(sid)


async def test_a_second_turn_while_one_runs_is_busy(api_client, seeded, offline, monkeypatch):
    import asyncio

    from services.chat import mock_model

    gate = asyncio.Event()
    original = mock_model.MockStream.__aenter__

    async def slow_enter(self):
        await gate.wait()
        return await original(self)

    monkeypatch.setattr(mock_model.MockStream, "__aenter__", slow_enter)
    made = await create_chat(api_client, seeded.user)
    sid = made["id"]
    first = asyncio.create_task(send_turn(api_client, seeded.user, sid, "A bioequivalence study of metformin"))
    repo = SqlChatRepository()
    for _ in range(200):
        await asyncio.sleep(0.05)
        if await repo.is_turn_locked(sid):
            break
    assert await repo.is_turn_locked(sid)

    second = await send_turn(api_client, seeded.user, sid, "and another one")
    assert second.status_code == 409 and detail_code(second) == "busy"
    gate.set()
    done = await first
    assert done.status_code == 200 and names(parse_sse(done.text))[-1] == "turn_end"


async def test_an_action_with_an_old_spec_version_is_409_stale_spec(api_client, seeded, offline):
    made = await create_chat(api_client, seeded.user)
    sid = made["id"]
    await send_turn(api_client, seeded.user, sid, "A bioequivalence study of metformin")
    detail = await get_chat(api_client, seeded.user, sid)
    matches = active(detail["cards"], "drug_matches")
    current = detail["spec"]["spec_version"]
    assert current > 0

    stale = await click(
        api_client, seeded.user, sid, matches, "select_drug",
        {"drug_id": matches["data"]["matches"][0]["id"]}, spec_version=current - 1,
    )
    assert stale.status_code == 409 and detail_code(stale) == "stale_spec"
    # Nothing changed, and the fresh version still works.
    assert (await get_chat(api_client, seeded.user, sid))["spec"]["spec_version"] == current
    ok = await click(
        api_client, seeded.user, sid, matches, "select_drug",
        {"drug_id": matches["data"]["matches"][0]["id"]}, spec_version=current,
    )
    assert ok.status_code == 200, ok.text


async def test_the_per_session_cap_is_429(api_client, seeded, offline, monkeypatch):
    monkeypatch.setenv("CHAT_SESSION_TURN_CAP", "2")
    made = await create_chat(api_client, seeded.user)
    sid = made["id"]
    for text in ("A bioequivalence study of metformin", "make it a food effect study"):
        assert (await send_turn(api_client, seeded.user, sid, text)).status_code == 200
    third = await send_turn(api_client, seeded.user, sid, "one more please")
    assert third.status_code == 429 and detail_code(third) == "session_cap"
    # Card clicks do not count against the cap.
    detail = await get_chat(api_client, seeded.user, sid)
    matches = active(detail["cards"], "drug_matches")
    assert (await click(
        api_client, seeded.user, sid, matches, "select_drug", {"drug_id": matches["data"]["matches"][0]["id"]},
    )).status_code == 200


async def test_the_per_day_cap_is_429_across_sessions(api_client, seeded, offline, monkeypatch):
    monkeypatch.setenv("CHAT_DAILY_TURN_CAP", "3")
    first = await create_chat(api_client, seeded.user)
    second = await create_chat(api_client, seeded.user)
    assert (await send_turn(api_client, seeded.user, first["id"], "A bioequivalence study of metformin")).status_code == 200
    assert (await send_turn(api_client, seeded.user, first["id"], "male subjects")).status_code == 200
    assert (await send_turn(api_client, seeded.user, second["id"], "A bioequivalence study of metformin")).status_code == 200
    blocked = await send_turn(api_client, seeded.user, second["id"], "one more")
    assert blocked.status_code == 429 and detail_code(blocked) == "daily_cap"
    # Another user is unaffected.
    other = await add_user(seeded.db, "other@testplatform.com")
    mine = await create_chat(api_client, other)
    assert (await send_turn(api_client, other, mine["id"], "A bioequivalence study of metformin")).status_code == 200


async def test_a_stored_first_message_may_be_resumed_even_at_the_cap(api_client, seeded, offline, monkeypatch):
    monkeypatch.setenv("CHAT_SESSION_TURN_CAP", "1")
    made = await create_chat(api_client, seeded.user, initial_message="A bioequivalence study of metformin")
    resumed = await send_turn(api_client, seeded.user, made["id"], resume=True)
    assert resumed.status_code == 200 and names(parse_sse(resumed.text))[-1] == "turn_end"
    assert (await send_turn(api_client, seeded.user, made["id"], "again")).status_code == 429


async def test_message_length_limit_is_4000_characters(api_client, seeded, offline):
    made = await create_chat(api_client, seeded.user)
    sid = made["id"]
    too_long = await send_turn(api_client, seeded.user, sid, "x" * 4001)
    assert too_long.status_code == 422
    assert (await send_turn(api_client, seeded.user, sid, "   ")).status_code == 422
    exactly = await send_turn(api_client, seeded.user, sid, "m" * 4000)
    assert exactly.status_code == 200
    # A bad body is rejected before the lock is taken.
    assert not await SqlChatRepository().is_turn_locked(sid)
    both = await api_client.post(
        f"/api/chat/{sid}/turns", json={"text": "hi", "resume": True, "client_turn_id": "a"},
        headers=as_user(seeded.user),
    )
    assert both.status_code == 422
    too_long_initial = await api_client.post(
        "/api/chat", json={"kind": "protocol", "initial_message": "y" * 4001}, headers=as_user(seeded.user),
    )
    assert too_long_initial.status_code == 422


async def test_resume_with_nothing_to_answer_is_an_error_event_not_a_crash(api_client, seeded, offline):
    made = await create_chat(api_client, seeded.user)
    resp = await send_turn(api_client, seeded.user, made["id"], resume=True)
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    err = [d for n, d in events if n == "error"]
    assert err and err[0]["code"] == "invalid_action"
    assert not await SqlChatRepository().is_turn_locked(made["id"])


# ---------------------------------------------------------------------------
# Clerk auth on: identity is the verified session, never X-User-Id
# ---------------------------------------------------------------------------


def _enable_clerk(monkeypatch):
    import auth

    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "_verify", lambda token: {"sub": f"clerk_{token}", "email": token})


async def test_with_clerk_on_every_route_needs_a_session_and_binds_identity(api_client, seeded, monkeypatch):
    _enable_clerk(monkeypatch)
    for resp in (
        await api_client.post("/api/chat", json={"kind": "budget"}),
        await api_client.get("/api/chat"),
        await api_client.get("/api/chat/stream-check"),
        await api_client.get(f"/api/chat/{uuid.uuid4()}"),
    ):
        assert resp.status_code == 401

    bearer = {"Authorization": f"Bearer {seeded.user.email}"}
    colleague = await add_user(seeded.db, "colleague@testplatform.com")
    # A spoofed X-User-Id is ignored: the verified email decides who the caller is.
    spoofed = {**bearer, "X-User-Id": str(colleague.id)}
    made = await api_client.post("/api/chat", json={"kind": "budget"}, headers=spoofed)
    assert made.status_code == 201, made.text
    sid = made.json()["id"]
    row = (await api_client.get(f"/api/chat/{sid}", headers=bearer))
    assert row.status_code == 200
    theirs = await api_client.get(
        f"/api/chat/{sid}", headers={"Authorization": f"Bearer {colleague.email}", "X-User-Id": str(seeded.user.id)},
    )
    assert theirs.status_code == 403
