"""Chat API (B4): create a session, run a turn over the SSE stream with the mock
model, walk the whole protocol flow with card clicks ending in
``generate_protocol`` (which completes synchronously under pytest), and check the
transcript, cards and spec persist and that GET refreshes the progress card to
protocol_ready.

Everything is offline and credit-free: the model is the MOCK_AI scripted client,
the literature services are stubbed, and generation uses the existing mock
pipeline. This module also holds the small helpers the other chat API test files
import (SSE parsing, session / click helpers, the ``offline`` fixture).
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from models import (
    AuditLog,
    ChatCard,
    ChatMessage,
    ChatSession,
    Drug,
    LlmUsage,
    PKParameter,
    PKParameterRecord,
    Protocol,
    ProtocolVersion,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers (imported by the sibling chat test files)
# ---------------------------------------------------------------------------


def as_user(user) -> dict[str, str]:
    return {"X-User-Id": str(user.id)}


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """``event:`` / ``data:`` frames of an SSE body, comments (``: ping``) skipped."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        name = None
        data: list[str] = []
        for line in block.split("\n"):
            if line.startswith(":") or not line:
                continue
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:"):].strip())
        if name:
            events.append((name, json.loads("".join(data)) if data else {}))
    return events


def names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [n for n, _ in events]


@pytest.fixture()
def offline(monkeypatch):
    """No network: PubMed / DailyMed / ClinicalTrials.gov return nothing, so the
    pipelines run on the engines alone (what MOCK_AI generation does anyway)."""
    from services.literature_service import LiteratureService

    async def _none(self, *a, **k):
        return None

    async def _empty(self, *a, **k):
        return []

    monkeypatch.setattr(LiteratureService, "fetch_pubmed", _empty)
    monkeypatch.setattr(LiteratureService, "fetch_dailymed", _none)
    monkeypatch.setattr(LiteratureService, "fetch_clinicaltrials", _empty)


async def add_drug(db, inn: str, brands: list[str] | None = None, *, t_half: float | None = None) -> Drug:
    drug = Drug(id=uuid.uuid4(), inn=inn, brand_names=brands or [], rxnorm_id=None)
    db.add(drug)
    await db.flush()
    if t_half is not None:
        db.add(PKParameterRecord(
            id=uuid.uuid4(), drug_id=drug.id, parameter=PKParameter.t_half, value=t_half, unit="h",
            population="healthy volunteers", source_pmid="1", confidence=1.0,
        ))
    await db.commit()
    await db.refresh(drug)
    return drug


async def create_chat(client, user, kind: str = "protocol", **extra) -> dict[str, Any]:
    resp = await client.post("/api/chat", json={"kind": kind, **extra}, headers=as_user(user))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def send_turn(client, user, sid: str, text: str | None = None, *, resume: bool = False,
                    turn_id: str | None = None):
    body: dict[str, Any] = {"client_turn_id": turn_id or uuid.uuid4().hex}
    if resume:
        body["resume"] = True
    else:
        body["text"] = text
    return await client.post(f"/api/chat/{sid}/turns", json=body, headers=as_user(user))


async def get_chat(client, user, sid: str) -> dict[str, Any]:
    resp = await client.get(f"/api/chat/{sid}", headers=as_user(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def click(client, user, sid: str, card: dict, action: str, payload: dict | None = None,
                *, spec_version: int | None = None):
    if spec_version is None:
        spec_version = (await get_chat(client, user, sid))["spec"]["spec_version"]
    return await client.post(
        f"/api/chat/{sid}/actions",
        json={"card_id": card["id"], "action": action, "payload": payload or {}, "spec_version": spec_version},
        headers=as_user(user),
    )


def active(cards: list[dict], card_type: str) -> dict | None:
    found = [x for x in cards if x["type"] == card_type and x["status"] == "active"]
    return found[-1] if found else None


def assert_no_randomization(*texts: str) -> None:
    for text in texts:
        assert "_randomization" not in text


async def drive_to_summary(client, user, sid: str, *, study_type: str = "BE", guideline: str = "FDA-BE-2014") -> dict:
    """Follow the active cards with clicks only (no model), the way the UI does,
    until the protocol_summary card is ready. Returns that card."""
    for _ in range(14):
        detail = await get_chat(client, user, sid)
        cards = detail["cards"]
        summary = active(cards, "protocol_summary")
        if summary and summary["data"]["ready"]:
            return summary
        design = active(cards, "design_decisions")
        population = active(cards, "population")
        indication = active(cards, "indication")
        study = active(cards, "study_type")
        pick = active(cards, "guideline")
        if design and design["actions"]:
            resp = await click(client, user, sid, design, "confirm_design")
        elif population:
            resp = await click(
                client, user, sid, population, "update_population", {"patch": population["data"]["confirm_patch"]},
            )
        elif indication and indication["data"].get("applicable"):
            resp = await click(
                client, user, sid, indication, "set_indication", {"text": indication["data"]["recommended"]["indication"]},
            )
        elif study and "study_type" in detail["spec"]["missing"]:
            resp = await click(client, user, sid, study, "set_study_type", {"study_type": study_type})
        elif pick:
            resp = await click(client, user, sid, pick, "set_guideline", {"guideline_version": guideline})
        else:
            raise AssertionError(
                f"no next click: missing={detail['spec']['missing']} "
                f"cards={[x['type'] + ':' + x['status'] for x in cards]}"
            )
        assert resp.status_code == 200, resp.text
        assert_no_randomization(resp.text)
    raise AssertionError("never reached a ready summary")


# ---------------------------------------------------------------------------
# Create / list / read
# ---------------------------------------------------------------------------


async def test_create_session_stores_an_unprocessed_first_message(api_client, seeded):
    made = await create_chat(
        api_client, seeded.user, initial_message="  Bioequivalence study for metformin 850 mg tablets  ",
    )
    assert made["kind"] == "protocol" and made["status"] == "active"
    assert made["title"] == "Bioequivalence study for metformin 850 mg tablets"
    assert made["spec_version"] == 0 and made["protocol_id"] is None

    detail = await get_chat(api_client, seeded.user, made["id"])
    assert detail["awaiting_reply"] is True
    assert [m["role"] for m in detail["messages"]] == ["user"]
    assert detail["messages"][0]["text"] == "Bioequivalence study for metformin 850 mg tablets"
    assert detail["cards"] == []
    # No model was called to create the session.
    usage = (await seeded.db.execute(select(LlmUsage))).scalars().all()
    assert usage == []


async def test_create_validates_the_kind_and_protocol_id_combination(api_client, seeded):
    h = as_user(seeded.user)
    pid = str(uuid.uuid4())
    for body in (
        {"kind": "protocol_edit"},                       # an edit chat needs its protocol
        {"kind": "protocol", "protocol_id": pid},        # a new-protocol chat has none
        {"kind": "protocol_edit", "protocol_id": "nope"},
        {"kind": "something-else"},
        {"kind": "protocol", "surprise": True},          # unknown keys are rejected
    ):
        assert (await api_client.post("/api/chat", json=body, headers=h)).status_code == 422, body
    # An unauthenticated caller (no X-User-Id, auth off) cannot create a session.
    assert (await api_client.post("/api/chat", json={"kind": "budget"})).status_code == 401


async def test_list_returns_my_sessions_newest_first_and_filters_by_kind(api_client, seeded):
    a = await create_chat(api_client, seeded.user, initial_message="first")
    b = await create_chat(api_client, seeded.user, kind="budget")
    resp = await api_client.get("/api/chat", headers=as_user(seeded.user))
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()][:2] == [b["id"], a["id"]]
    only_budget = await api_client.get("/api/chat?kind=budget", headers=as_user(seeded.user))
    assert [r["id"] for r in only_budget.json()] == [b["id"]]
    too_many = await api_client.get("/api/chat?limit=1000", headers=as_user(seeded.user))
    assert too_many.status_code == 422


async def test_patch_renames_and_archives(api_client, seeded):
    made = await create_chat(api_client, seeded.user)
    resp = await api_client.patch(
        f"/api/chat/{made['id']}", json={"title": "My adalimumab study", "status": "archived"},
        headers=as_user(seeded.user),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "My adalimumab study" and resp.json()["status"] == "archived"
    # "completed" is server-controlled; an empty patch is rejected.
    assert (await api_client.patch(
        f"/api/chat/{made['id']}", json={"status": "completed"}, headers=as_user(seeded.user),
    )).status_code == 422
    assert (await api_client.patch(
        f"/api/chat/{made['id']}", json={}, headers=as_user(seeded.user),
    )).status_code == 422


# ---------------------------------------------------------------------------
# A turn over the stream
# ---------------------------------------------------------------------------


async def test_turn_streams_events_with_the_mock_model_and_persists_everything(api_client, seeded, offline):
    made = await create_chat(api_client, seeded.user, initial_message="Bioequivalence study for metformin 850 mg tablets")
    sid = made["id"]
    resp = await send_turn(api_client, seeded.user, sid, resume=True)

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache, no-transform"
    assert resp.headers["x-accel-buffering"] == "no"
    events = parse_sse(resp.text)
    kinds = names(events)
    assert kinds[0] == "turn_start" and kinds[-1] == "turn_end"
    assert "text_delta" in kinds and "tool_start" in kinds and "card" in kinds and "spec" in kinds
    assert "error" not in kinds
    cards_in_stream = [d for n, d in events if n == "card"]
    assert {x["type"] for x in cards_in_stream} >= {"drug_matches"}
    assert_no_randomization(resp.text)

    detail = await get_chat(api_client, seeded.user, sid)
    assert detail["awaiting_reply"] is False
    roles = [m["role"] for m in detail["messages"]]
    assert roles[0] == "user" and "assistant" in roles
    assert any(m["card_ids"] for m in detail["messages"] if m["role"] == "assistant")
    assert {x["type"] for x in detail["cards"]} >= {"drug_matches"}
    assert detail["spec"]["spec_version"] > 0
    fields = {f["path"]: f for f in detail["spec"]["fields"]}
    assert fields["study_type"]["display_value"]
    # The lock was released and the usage ledger got a row.
    row = await seeded.db.get(ChatSession, uuid.UUID(sid))
    await seeded.db.refresh(row)
    assert row.turn_lock_id is None
    ledger = (await seeded.db.execute(select(LlmUsage))).scalars().all()
    assert len(ledger) >= 1 and all(r.feature.value == "chat" for r in ledger)
    assert_no_randomization(json.dumps(detail))


async def test_a_text_turn_stores_the_user_message_once_per_client_turn_id(api_client, seeded, offline):
    made = await create_chat(api_client, seeded.user)
    sid = made["id"]
    first = await send_turn(api_client, seeded.user, sid, "A bioequivalence study of metformin", turn_id="turn-1")
    assert names(parse_sse(first.text))[-1] == "turn_end"

    again = await send_turn(api_client, seeded.user, sid, "A bioequivalence study of metformin", turn_id="turn-1")
    assert again.status_code == 200
    replay = parse_sse(again.text)
    assert names(replay) == ["turn_start", "turn_end"]  # answered already: nothing re-runs

    rows = (await seeded.db.execute(
        select(ChatMessage).where(ChatMessage.session_id == uuid.UUID(sid), ChatMessage.role == "user")
    )).scalars().all()
    assert len(rows) == 1


async def test_a_disconnect_does_not_stop_the_turn(api_client, seeded, offline, monkeypatch):
    """Cancelling the HTTP request (the browser went away) must not cancel the
    turn: it still finishes, saves its messages and releases the lock."""
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
    request = asyncio.create_task(send_turn(api_client, seeded.user, sid, "A bioequivalence study of metformin"))
    for _ in range(200):  # wait until the turn holds the lock
        await asyncio.sleep(0.05)
        row = await seeded.db.get(ChatSession, uuid.UUID(sid))
        await seeded.db.refresh(row)
        if row.turn_lock_id:
            break
    assert row.turn_lock_id, "the turn never started"
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    gate.set()
    for _ in range(400):
        await asyncio.sleep(0.05)
        await seeded.db.refresh(row)
        if row.turn_lock_id is None:
            break
    assert row.turn_lock_id is None, "the turn did not finish after the disconnect"
    detail = await get_chat(api_client, seeded.user, sid)
    assert "assistant" in [m["role"] for m in detail["messages"]]
    assert {x["type"] for x in detail["cards"]} >= {"drug_matches"}


# ---------------------------------------------------------------------------
# The whole protocol flow, ending in generate_protocol
# ---------------------------------------------------------------------------


async def test_full_protocol_flow_ends_in_a_generated_protocol(api_client, seeded, offline):
    user, client = seeded.user, api_client
    made = await create_chat(client, user, initial_message="Bioequivalence study for metformin 850 mg tablets")
    sid = made["id"]

    # 1. The assistant finds the drug and records the study type.
    turn = await send_turn(client, user, sid, resume=True)
    assert "error" not in names(parse_sse(turn.text))
    detail = await get_chat(client, user, sid)
    matches = active(detail["cards"], "drug_matches")
    assert matches and matches["data"]["matches"][0]["inn"] == "metformin"

    # 2. Clicks only from here on (no model): pick the drug, then follow the cards.
    drug_id = matches["data"]["matches"][0]["id"]
    resp = await click(client, user, sid, matches, "select_drug", {"drug_id": drug_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["guide"]["text"]
    assert_no_randomization(resp.text)

    await drive_to_summary(client, user, sid)

    detail = await get_chat(client, user, sid)
    summary = active(detail["cards"], "protocol_summary")
    assert summary and summary["data"]["ready"] and not summary["data"]["blocking_issue"]
    assert [a["id"] for a in summary["actions"]] == ["generate_protocol"]

    # 3. Generate: the same gates as POST /api/protocols/; synchronous under pytest.
    resp = await click(client, user, sid, summary, "generate_protocol")
    assert resp.status_code == 200, resp.text
    done = resp.json()
    types = {x["type"]: x for x in done["cards"]}
    assert types["generation_progress"]["status"] == "done"
    assert types["generation_progress"]["data"]["status"] == "complete"
    assert types["protocol_ready"]["data"]["section_count"] == 17
    protocol_id = types["protocol_ready"]["data"]["protocol_id"]
    assert_no_randomization(resp.text)

    # 4. The protocol exists, and its INITIAL snapshot carries the chat provenance.
    protocol = await seeded.db.get(Protocol, uuid.UUID(protocol_id))
    assert protocol is not None and protocol.generation_status == "complete"
    snap = (await seeded.db.execute(
        select(ProtocolVersion).where(ProtocolVersion.protocol_id == protocol.id, ProtocolVersion.version == 1)
    )).scalar_one()
    assert snap.change_detail["source"] == "chat"
    assert snap.change_detail["chat_session_id"] == sid
    assert "study_type" in snap.change_detail["field_provenance"]

    # 5. GET refreshes to protocol_ready; transcript + spec persisted across GETs.
    detail = await get_chat(client, user, sid)
    by_type = {x["type"]: x for x in detail["cards"]}
    assert by_type["protocol_ready"]["data"]["protocol_id"] == protocol_id
    assert by_type["generation_progress"]["status"] == "done"
    assert detail["session"]["protocol_id"] == protocol_id
    roles = [m["role"] for m in detail["messages"]]
    assert roles.count("ui_action") >= 5 and "assistant" in roles and roles[0] == "user"
    # Cards created by a click hang off a transcript line, so a reload can place them.
    placed = {cid for m in detail["messages"] for cid in m["card_ids"]}
    assert {by_type["protocol_ready"]["id"], by_type["generation_progress"]["id"]} <= placed
    assert_no_randomization(json.dumps(detail))

    # 6. The spec survived: a second read is identical.
    again = await get_chat(client, user, sid)
    assert again["spec"] == detail["spec"] and [m["id"] for m in again["messages"]] == [m["id"] for m in detail["messages"]]

    # 7. No extra credits were spent and the audit trail names the chat session.
    audit = (await seeded.db.execute(
        select(AuditLog).where(AuditLog.resource_type == "chat_session", AuditLog.resource_id == sid)
    )).scalars().all()
    assert any(a.action == "POST:chat_session" for a in audit)
    created = (await seeded.db.execute(select(AuditLog).where(AuditLog.action == "CHAT_CREATE:protocol"))).scalars().all()
    assert len(created) == 1 and created[0].resource_id == protocol_id and created[0].diff["chat_session_id"] == sid
    assert created[0].user_id == user.id


async def test_generation_progress_card_refreshes_from_the_protocol_status(api_client, seeded, offline, monkeypatch):
    """The production path: generation is a background task, the action answers
    'generating', and GET /api/chat/{sid} flips the card (and adds protocol_ready)
    once the protocol row is complete."""
    from routers import protocols as protocols_router

    monkeypatch.setattr(protocols_router, "_generate_synchronously", lambda: False)
    monkeypatch.setattr(protocols_router, "_spawn_generation", lambda **kw: None)  # the task never runs

    user, client = seeded.user, api_client
    made = await create_chat(client, user)
    sid = made["id"]
    await send_turn(client, user, sid, "A bioequivalence study of metformin")
    detail = await get_chat(client, user, sid)
    matches = active(detail["cards"], "drug_matches")
    await click(client, user, sid, matches, "select_drug", {"drug_id": matches["data"]["matches"][0]["id"]})
    summary = await drive_to_summary(client, user, sid)
    resp = await click(client, user, sid, summary, "generate_protocol")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    progress = next(x for x in body["cards"] if x["type"] == "generation_progress")
    assert progress["status"] == "active" and progress["data"]["status"] == "generating"
    assert not any(x["type"] == "protocol_ready" for x in body["cards"])
    protocol_id = progress["data"]["protocol_id"]

    # Still generating: a GET changes nothing.
    detail = await get_chat(client, user, sid)
    assert not any(x["type"] == "protocol_ready" for x in detail["cards"])

    # The background task finishes (simulated): the row flips to complete.
    protocol = await seeded.db.get(Protocol, uuid.UUID(protocol_id))
    protocol.generation_status = "complete"
    protocol.sections = {"title": "<p>t</p>", "synopsis": "<p>s</p>"}
    await seeded.db.commit()

    detail = await get_chat(client, user, sid)
    by_type = {x["type"]: x for x in detail["cards"]}
    assert by_type["generation_progress"]["status"] == "done"
    assert by_type["protocol_ready"]["data"]["protocol_id"] == protocol_id
    # Idempotent: reading again does not add a second ready card.
    detail2 = await get_chat(client, user, sid)
    assert [x["id"] for x in detail2["cards"]].count(by_type["protocol_ready"]["id"]) == 1
    assert sum(1 for x in detail2["cards"] if x["type"] == "protocol_ready") == 1


async def test_concurrent_refreshes_announce_a_finished_protocol_once(api_client, seeded, offline, monkeypatch):
    """The chat UI polls GET /chat/{sid} while another poll is in flight; two
    refreshes that both see the protocol finish must not both write the
    'Your protocol is ready.' line (a duplicate showed the ready card twice)."""
    import asyncio

    from routers import protocols as protocols_router

    monkeypatch.setattr(protocols_router, "_generate_synchronously", lambda: False)
    monkeypatch.setattr(protocols_router, "_spawn_generation", lambda **kw: None)

    user, client = seeded.user, api_client
    sid = (await create_chat(client, user))["id"]
    await send_turn(client, user, sid, "A bioequivalence study of metformin")
    matches = active((await get_chat(client, user, sid))["cards"], "drug_matches")
    await click(client, user, sid, matches, "select_drug", {"drug_id": matches["data"]["matches"][0]["id"]})
    summary = await drive_to_summary(client, user, sid)
    body = (await click(client, user, sid, summary, "generate_protocol")).json()
    protocol_id = next(x for x in body["cards"] if x["type"] == "generation_progress")["data"]["protocol_id"]

    protocol = await seeded.db.get(Protocol, uuid.UUID(protocol_id))
    protocol.generation_status = "complete"
    protocol.sections = {"title": "<p>t</p>", "synopsis": "<p>s</p>"}
    await seeded.db.commit()

    await asyncio.gather(*(get_chat(client, user, sid) for _ in range(4)))
    detail = await get_chat(client, user, sid)
    ready_lines = [m for m in detail["messages"] if m["text"] == "Your protocol is ready."]
    assert len(ready_lines) == 1, [m["text"] for m in detail["messages"]]
    assert sum(1 for x in detail["cards"] if x["type"] == "protocol_ready") == 1


async def test_a_failed_generation_is_reported_on_the_progress_card(api_client, seeded, offline, monkeypatch):
    from routers import protocols as protocols_router

    monkeypatch.setattr(protocols_router, "_generate_synchronously", lambda: False)
    monkeypatch.setattr(protocols_router, "_spawn_generation", lambda **kw: None)
    user, client = seeded.user, api_client
    made = await create_chat(client, user)
    sid = made["id"]
    # Seed a progress card + protocol row directly.
    protocol = Protocol(
        id=uuid.uuid4(), org_id=user.org_id, drug_id=seeded.drug.id, study_type="BE", population_config={},
        sections={}, guideline_version="FDA-BE-2014", created_by=user.id, generation_status="failed",
        generation_error="Generation failed: out of credits.",
    )
    seeded.db.add(protocol)
    await seeded.db.commit()
    from services.chat import cards as K
    from services.chat.store import SqlChatRepository

    card = K.build_generation_progress_card(0, str(protocol.id), "metformin", "generating")
    await SqlChatRepository().upsert_card(sid, org_id=user.org_id, card=card)

    detail = await get_chat(client, user, sid)
    got = next(x for x in detail["cards"] if x["type"] == "generation_progress")
    assert got["status"] == "error"
    assert got["data"]["status"] == "failed" and "out of credits" in got["data"]["error"]
    assert not any(x["type"] == "protocol_ready" for x in detail["cards"])


async def test_add_drug_requires_a_click_and_selects_the_new_drug(api_client, seeded, offline):
    user, client = seeded.user, api_client
    made = await create_chat(client, user)
    sid = made["id"]
    # "warfarin" is a known name to the mock model but not in this test catalogue.
    await send_turn(client, user, sid, "A bioequivalence study of warfarin")
    detail = await get_chat(client, user, sid)
    offer = active(detail["cards"], "drug_add_offer")
    assert offer and offer["data"]["inn"] == "warfarin"
    assert (await seeded.db.execute(select(Drug).where(Drug.inn == "warfarin"))).scalar_one_or_none() is None

    resp = await click(client, user, sid, offer, "add_drug", {"inn": "warfarin"})
    assert resp.status_code == 200, resp.text
    drug = (await seeded.db.execute(select(Drug).where(Drug.inn == "warfarin"))).scalar_one()
    detail = await get_chat(client, user, sid)
    spec_fields = {f["path"]: f for f in detail["spec"]["fields"]}
    assert "warfarin" in (spec_fields["drug"]["display_value"] or "").lower()
    assert next(x for x in detail["cards"] if x["id"] == offer["id"])["status"] == "done"
    audit = (await seeded.db.execute(select(AuditLog).where(AuditLog.action == "CHAT_CREATE:drug"))).scalars().all()
    assert audit and audit[0].resource_id == str(drug.id)


async def test_a_biologic_cannot_be_generated_as_be_and_the_card_says_why(api_client, seeded, offline):
    """The server's own study-type gate wording is what the user sees; the draft
    never reaches generation."""
    user, client = seeded.user, api_client
    await add_drug(seeded.db, "adalimumab", ["Humira"])
    made = await create_chat(client, user)
    sid = made["id"]
    # The assistant records "bioequivalence" from the user's words, then the user
    # picks the biologic: the incompatible study type is cleared, not carried on.
    await send_turn(client, user, sid, "A bioequivalence study of adalimumab")
    detail = await get_chat(client, user, sid)
    fields = {f["path"]: f for f in detail["spec"]["fields"]}
    assert fields["study_type"]["display_value"] == "Bioequivalence"
    matches = active(detail["cards"], "drug_matches")
    picked = await click(client, user, sid, matches, "select_drug", {"drug_id": matches["data"]["matches"][0]["id"]})
    assert picked.status_code == 200, picked.text
    detail = await get_chat(client, user, sid)
    fields = {f["path"]: f for f in detail["spec"]["fields"]}
    assert fields["drug"]["display_value"] == "adalimumab" and fields["study_type"]["display_value"] is None

    study = active(detail["cards"], "study_type")
    options = {o["value"]: o for o in study["data"]["options"]}
    assert options["BE"]["allowed"] is False and "biologic" in options["BE"]["reason"]
    assert options["biosimilar_pk"]["allowed"] is True

    refused = await click(client, user, sid, study, "set_study_type", {"study_type": "BE"})
    assert refused.status_code == 422
    assert refused.json()["detail"]["message"] == (
        "'adalimumab' is a biologic and cannot run a 'BE' study; allowed study type(s): biosimilar_pk."
    )
    assert (await seeded.db.execute(select(Protocol))).scalars().all() == []


async def test_card_actions_are_refused_for_the_wrong_session_kind(api_client, seeded, offline):
    made = await create_chat(api_client, seeded.user, kind="budget")
    resp = await click(
        api_client, seeded.user, made["id"], {"id": str(uuid.uuid4())}, "set_study_type", {"study_type": "BE"},
        spec_version=0,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_action"


async def test_leftover_database_rows_do_not_leak_between_tests(seeded):
    """Sanity: the chat tables start empty in every test (fresh schema)."""
    for model in (ChatSession, ChatMessage, ChatCard):
        assert (await seeded.db.execute(select(model))).first() is None
