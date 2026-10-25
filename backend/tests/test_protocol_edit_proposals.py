"""Editing a finished protocol by chat (B9): proposals, diffs, apply and discard.

A chat never changes a protocol by itself. The model can only ask for an
``edit_impact`` card; the user's click creates a proposal (a background job
prepares the candidate sections / config), and a second click applies it, only if
the protocol is still at the version the proposal was built from and still a
draft. These tests drive the real HTTP routes with the MOCK_AI model, the
existing mock generation pipeline and mock section revisions (``[MOCK REVISION]``),
so the whole flow runs without credits or network.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from models import (
    AuditLog,
    PKParameter,
    PKParameterRecord,
    Protocol,
    ProtocolEditProposal,
    ProtocolStatus,
    ProtocolVersion,
)
from services.chat import edits
from tests.test_chat_api import (  # noqa: F401  (fixtures + helpers)
    active,
    add_drug,
    as_user,
    assert_no_randomization,
    click,
    create_chat,
    get_chat,
    names,
    offline,
    parse_sse,
    send_turn,
)

# (asyncio_mode = auto: the async tests need no marker; the merge tests at the end are plain functions.)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def add_pk(db, drug, t_half: float) -> None:
    db.add(PKParameterRecord(
        id=uuid.uuid4(), drug_id=drug.id, parameter=PKParameter.t_half, value=t_half, unit="h",
        population="healthy volunteers", source_pmid="1", confidence=1.0,
    ))
    await db.commit()


async def make_protocol(client, user, drug, study_type: str = "BE", **config) -> dict:
    body = {"drug_id": str(drug.id), "study_type": study_type, "population_config": {"dose_strength": "850 mg", **config}}
    resp = await client.post("/api/protocols/", json=body, headers=as_user(user))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def reload(db, model, pk):
    """A fresh read of one row (other sessions wrote it); never expire_all(), which
    would make later attribute access on other loaded objects try to do IO."""
    obj = await db.get(model, pk if isinstance(pk, uuid.UUID) else uuid.UUID(str(pk)))
    if obj is not None:
        await db.refresh(obj)
    return obj


async def propose(client, user, sid: str, text: str) -> dict:
    """Send one message and return the session detail afterwards."""
    turn = await send_turn(client, user, sid, text)
    assert turn.status_code == 200, turn.text
    events = parse_sse(turn.text)
    assert "error" not in names(events), events
    assert_no_randomization(turn.text)
    return await get_chat(client, user, sid)


async def edit_session(client, user, protocol_id: str) -> str:
    made = await create_chat(client, user, kind="protocol_edit", protocol_id=protocol_id)
    return made["id"]


async def request_prose_edit(client, user, sid: str, text: str = "shorten the adverse events section") -> dict:
    detail = await propose(client, user, sid, text)
    impact = active(detail["cards"], "edit_impact")
    assert impact, [f"{x['type']}:{x['status']}" for x in detail["cards"]]
    return impact


async def confirm(client, user, sid: str, impact: dict):
    return await click(client, user, sid, impact, "confirm_edit", {"proposal_id": impact["data"]["proposal_id"]})


def card_of(resp_json: dict, card_type: str) -> dict:
    return next(x for x in resp_json["cards"] if x["type"] == card_type)


async def prose_proposal_ready(client, user, sid: str) -> tuple[dict, dict]:
    impact = await request_prose_edit(client, user, sid)
    resp = await confirm(client, user, sid, impact)
    assert resp.status_code == 200, resp.text
    return impact, card_of(resp.json(), "edit_diff")


def text_of_last_assistant(detail: dict) -> str:
    return [m["text"] for m in detail["messages"] if m["role"] == "assistant" and m["text"]][-1]


# ---------------------------------------------------------------------------
# Prose edit: propose -> confirm -> diff -> apply
# ---------------------------------------------------------------------------


async def test_prose_edit_end_to_end(api_client, seeded, offline):
    user, client = seeded.user, api_client
    protocol = await make_protocol(client, user, seeded.drug)
    pid = protocol["id"]
    before = dict(protocol["sections"])
    config_before = (await reload(seeded.db, Protocol, pid)).population_config

    made = await create_chat(client, user, kind="protocol_edit", protocol_id=pid)
    sid = made["id"]
    assert made["protocol_id"] == pid and made["title"].startswith("Edit")
    detail0 = await get_chat(client, user, sid)
    fields = {f["path"]: f for f in detail0["spec"]["fields"]}
    assert fields["editable"]["display_value"] == "Yes"
    assert "metformin" in fields["protocol"]["display_value"]

    # 1. "shorten the adverse events section" -> an impact card, nothing changed yet.
    impact = await request_prose_edit(client, user, sid)
    assert impact["data"]["kind"] == "prose" and impact["data"]["sections"] == ["safety_monitoring"]
    assert impact["data"]["base_version"] == 1 and impact["data"]["editable"] is True
    assert [a["id"] for a in impact["actions"]] == ["confirm_edit", "discard_edit"]
    proposal_id = impact["data"]["proposal_id"]
    assert (await reload(seeded.db, Protocol, pid)).version == 1
    assert (await seeded.db.execute(select(ProtocolEditProposal))).scalars().all() == []

    # 2. Confirm: the proposal row gets the pre-minted id; the job runs inline under pytest.
    resp = await confirm(client, user, sid, impact)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_no_randomization(resp.text)
    progress = card_of(body, "edit_progress")
    assert progress["data"]["proposal_id"] == proposal_id and progress["data"]["status"] == "ready"
    assert next(x for x in body["cards"] if x["id"] == impact["id"])["status"] == "done"
    diff = card_of(body, "edit_diff")
    assert diff["data"]["status"] == "ready" and diff["data"]["kind"] == "prose"
    assert [s["section_name"] for s in diff["data"]["sections"]] == ["safety_monitoring"]
    assert "[MOCK REVISION]" in diff["data"]["sections"][0]["new_content"]
    assert diff["data"]["sections"][0]["old_content"] == before["safety_monitoring"]
    assert diff["data"]["qc"]["total_checks"] > 0
    assert diff["data"]["facts_before"] == [] and diff["data"]["facts_after"] == []  # prose: engine facts untouched
    assert [a["id"] for a in diff["actions"]] == ["apply_edit", "discard_edit"]
    row = await reload(seeded.db, ProtocolEditProposal, proposal_id)
    assert row.status.value == "ready" and row.kind.value == "prose" and row.base_version == 1
    assert str(row.session_id) == sid and set(row.proposed_sections) == set(before)
    assert row.proposed_config is None
    assert (await reload(seeded.db, Protocol, pid)).version == 1  # still nothing applied

    # 3. The polling endpoint agrees.
    polled = await client.get(f"/api/chat/{sid}/proposals/{proposal_id}", headers=as_user(user))
    assert polled.status_code == 200, polled.text
    pj = polled.json()
    assert pj["status"] == "ready" and pj["kind"] == "prose" and pj["base_version"] == 1 and pj["applied_version"] is None
    assert [d["section_name"] for d in pj["diff"]] == ["safety_monitoring"]
    assert pj["qc"]["total_checks"] > 0 and pj["request"]["instruction"]
    assert_no_randomization(polled.text)

    # 4. Apply: version + 1, a CHAT_EDIT snapshot, an audit row.
    resp = await click(client, user, sid, diff, "apply_edit", {"proposal_id": proposal_id})
    assert resp.status_code == 200, resp.text
    applied = next(x for x in resp.json()["cards"] if x["type"] == "edit_diff" and x["data"]["status"] == "applied")
    assert applied["data"]["applied_version"] == 2 and applied["status"] == "done"
    assert_no_randomization(resp.text)

    stored = await reload(seeded.db, Protocol, pid)
    assert stored.version == 2 and stored.status == ProtocolStatus.draft
    assert "[MOCK REVISION]" in stored.sections["safety_monitoring"]
    assert {k: v for k, v in stored.sections.items() if k != "safety_monitoring"} == {
        k: v for k, v in before.items() if k != "safety_monitoring"
    }
    assert stored.population_config == config_before  # a prose edit never touches the engine config

    snap = (await seeded.db.execute(
        select(ProtocolVersion).where(ProtocolVersion.protocol_id == stored.id, ProtocolVersion.version == 2)
    )).scalar_one()
    assert snap.change_type == "CHAT_EDIT" and snap.changed_by == user.id
    assert snap.change_detail["proposal_id"] == proposal_id and snap.change_detail["session_id"] == sid
    assert snap.change_detail["kind"] == "prose" and snap.change_detail["sections"] == ["safety_monitoring"]
    assert snap.change_detail["request"]["sections"] == ["safety_monitoring"]
    audit = (await seeded.db.execute(select(AuditLog).where(AuditLog.action == "CHAT_EDIT:protocol"))).scalars().all()
    assert len(audit) == 1 and audit[0].resource_id == pid and audit[0].user_id == user.id
    assert audit[0].diff["proposal_id"] == proposal_id and audit[0].diff["version"] == 2

    row = await reload(seeded.db, ProtocolEditProposal, proposal_id)
    assert row.status.value == "applied" and row.applied_version == 2
    versions = await client.get(f"/api/protocols/{pid}/versions", headers=as_user(user))
    assert versions.json()[0]["change_type"] == "CHAT_EDIT"

    # 5. It cannot be applied twice, and the transcript shows what happened.
    again = await click(client, user, sid, diff, "apply_edit", {"proposal_id": proposal_id})
    assert again.status_code == 422 and again.json()["detail"]["code"] == "invalid_action"
    assert (await reload(seeded.db, Protocol, pid)).version == 2
    detail = await get_chat(client, user, sid)
    assert detail["spec"]["spec_version"] >= 3
    assert_no_randomization(json.dumps(detail))


async def test_discard_before_confirm_and_after_ready(api_client, seeded, offline):
    user, client = seeded.user, api_client
    protocol = await make_protocol(client, user, seeded.drug)
    pid = protocol["id"]
    sid = await edit_session(client, user, pid)

    # Cancel on the impact card: nothing was created.
    impact = await request_prose_edit(client, user, sid)
    resp = await click(client, user, sid, impact, "discard_edit", {"proposal_id": impact["data"]["proposal_id"]})
    assert resp.status_code == 200, resp.text
    assert next(x for x in resp.json()["cards"] if x["id"] == impact["id"])["status"] == "done"
    assert (await seeded.db.execute(select(ProtocolEditProposal))).scalars().all() == []
    # The cancelled request cannot be confirmed any more.
    late = await confirm(client, user, sid, impact)
    assert late.status_code == 422

    # Discard a ready proposal: it is marked discarded and can never be applied.
    impact2, diff = await prose_proposal_ready(client, user, sid)
    proposal_id = impact2["data"]["proposal_id"]
    resp = await click(client, user, sid, diff, "discard_edit", {"proposal_id": proposal_id})
    assert resp.status_code == 200, resp.text
    assert (await reload(seeded.db, ProtocolEditProposal, proposal_id)).status.value == "discarded"
    refused = await click(client, user, sid, diff, "apply_edit", {"proposal_id": proposal_id})
    assert refused.status_code == 422
    assert (await reload(seeded.db, Protocol, pid)).version == 1
    polled = await client.get(f"/api/chat/{sid}/proposals/{proposal_id}", headers=as_user(user))
    assert polled.json()["status"] == "discarded" and polled.json()["diff"] == []


# ---------------------------------------------------------------------------
# Engine edits: a re-run of the generation with the changed inputs
# ---------------------------------------------------------------------------


async def test_engine_edit_with_a_valid_washout_on_a_crossover_drug(api_client, seeded, offline):
    user, client = seeded.user, api_client
    await add_pk(seeded.db, seeded.drug, 24.0)  # 7 x t1/2 = 7 days
    protocol = await make_protocol(client, user, seeded.drug)
    pid = protocol["id"]
    stored = await reload(seeded.db, Protocol, pid)
    ss = stored.population_config["_sample_size"]
    assert ss["design_key"] == "crossover" and ss["washout_days"] == 7.0 and not ss["washout_is_user_override"]
    seed_before = stored.population_config["_randomization_seed"]
    alloc_before = stored.population_config["_randomization"]

    sid = await edit_session(client, user, pid)
    detail = await propose(client, user, sid, "make the washout 14 days")
    impact = active(detail["cards"], "edit_impact")
    assert impact, text_of_last_assistant(detail)
    assert impact["data"]["kind"] == "engine"
    change = impact["data"]["changes"][0]
    assert change["field"] == "washout_days" and change["after"] == "14"
    assert any("locked" in w for w in impact["data"]["warnings"])
    assert "re-runs the full generation" in impact["data"]["rerun_note"]

    resp = await confirm(client, user, sid, impact)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_no_randomization(resp.text)
    diff = card_of(body, "edit_diff")
    assert diff["data"]["kind"] == "engine" and diff["data"]["status"] == "ready"
    before = {r["label"]: r["value"] for r in diff["data"]["facts_before"]}
    after = {r["label"]: r["value"] for r in diff["data"]["facts_after"]}
    assert before["Washout between periods"] == "7 days" and after["Washout between periods"] == "14 days"
    assert before["Design"] == after["Design"]
    assert "Planned enrolment" in after
    proposal_id = impact["data"]["proposal_id"]
    row = await reload(seeded.db, ProtocolEditProposal, proposal_id)
    assert row.status.value == "ready" and row.proposed_config["_sample_size"]["washout_days"] == 14.0
    # Nothing is applied until the click.
    assert (await reload(seeded.db, Protocol, pid)).population_config["_sample_size"]["washout_days"] == 7.0

    polled = await client.get(f"/api/chat/{sid}/proposals/{proposal_id}", headers=as_user(user))
    pj = polled.json()
    assert pj["facts_before"]["Washout between periods"] == "7 days"
    assert pj["facts_after"]["Washout between periods"] == "14 days"
    assert_no_randomization(polled.text)

    resp = await click(client, user, sid, diff, "apply_edit", {"proposal_id": proposal_id})
    assert resp.status_code == 200, resp.text
    assert_no_randomization(resp.text)

    stored = await reload(seeded.db, Protocol, pid)
    assert stored.version == 2
    ss = stored.population_config["_sample_size"]
    assert ss["washout_days"] == 14.0 and ss["washout_is_user_override"] is True
    assert stored.population_config["washout_days"] == 14.0
    assert stored.population_config["dose_strength"] == "850 mg"  # unrelated inputs carried over
    # The same randomization seed: the filed allocation list stays reproducible.
    assert stored.population_config["_randomization_seed"] == seed_before
    assert stored.population_config["_randomization"] == alloc_before
    snap = (await seeded.db.execute(
        select(ProtocolVersion).where(ProtocolVersion.protocol_id == stored.id, ProtocolVersion.version == 2)
    )).scalar_one()
    assert snap.change_type == "CHAT_EDIT" and snap.change_detail["kind"] == "engine"
    assert snap.change_detail["request"]["changes"] == {"washout_days": 14.0}
    # The audit record says what the re-run did, in the same keys as the INITIAL snapshot.
    assert set(snap.change_detail["generated_sections"]) == set(stored.sections)
    audit = (await seeded.db.execute(select(AuditLog).where(AuditLog.action == "CHAT_EDIT:protocol"))).scalars().all()
    assert len(audit) == 1
    # The engine's CV override works the same way (a different input, same path).
    detail = await propose(client, user, sid, "set the CV to 30%")
    impact = active(detail["cards"], "edit_impact")
    assert impact and impact["data"]["changes"][0]["field"] == "cv_intra" and impact["data"]["estimate"]


async def test_washout_is_refused_on_a_parallel_biologic_design(api_client, seeded, offline):
    user, client = seeded.user, api_client
    ada = await add_drug(seeded.db, "adalimumab", ["Humira"])
    protocol = await make_protocol(client, user, ada, "biosimilar_pk")
    assert (await reload(seeded.db, Protocol, protocol["id"])).population_config["_sample_size"]["design_key"] == "parallel"
    sid = await edit_session(client, user, protocol["id"])

    detail = await propose(client, user, sid, "make the washout 14 days")
    assert active(detail["cards"], "edit_impact") is None
    assert "no washout" in text_of_last_assistant(detail).lower()
    assert (await seeded.db.execute(select(ProtocolEditProposal))).scalars().all() == []
    assert (await reload(seeded.db, Protocol, protocol["id"])).version == 1


async def test_washout_below_the_engine_minimum_is_refused_with_the_minimum_stated(api_client, seeded, offline):
    user, client = seeded.user, api_client
    await add_pk(seeded.db, seeded.drug, 24.0)
    protocol = await make_protocol(client, user, seeded.drug)
    sid = await edit_session(client, user, protocol["id"])
    detail = await propose(client, user, sid, "make the washout 3 days")
    assert active(detail["cards"], "edit_impact") is None
    reply = text_of_last_assistant(detail)
    assert "7 days" in reply and "minimum" in reply.lower()


async def test_the_washout_is_rechecked_at_confirm_time_before_any_credits_are_spent(api_client, seeded, offline):
    user, client = seeded.user, api_client
    await add_pk(seeded.db, seeded.drug, 24.0)
    protocol = await make_protocol(client, user, seeded.drug)
    pid = protocol["id"]
    sid = await edit_session(client, user, pid)
    detail = await propose(client, user, sid, "make the washout 14 days")
    impact = active(detail["cards"], "edit_impact")
    assert impact

    # The stored half-life moves (e.g. the protocol was re-sized): 14 days is now below 7 x t1/2 = 21 days.
    stored = await reload(seeded.db, Protocol, pid)
    cfg = dict(stored.population_config)
    cfg["_sample_size"] = {**cfg["_sample_size"], "t_half_hours": 72.0}
    stored.population_config = cfg
    await seeded.db.commit()

    resp = await confirm(client, user, sid, impact)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "validation"
    assert "minimum" in resp.json()["detail"]["message"] and "21 days" in resp.json()["detail"]["message"]
    assert (await seeded.db.execute(select(ProtocolEditProposal))).scalars().all() == []  # no generation was started


# ---------------------------------------------------------------------------
# Stale proposals and locked protocols
# ---------------------------------------------------------------------------


async def manual_edit(client, user, pid: str, section: str = "synopsis", content: str = "<p>Edited by hand.</p>") -> None:
    resp = await client.put(f"/api/protocols/{pid}/sections/{section}", json={"content": content}, headers=as_user(user))
    assert resp.status_code == 200, resp.text


async def test_apply_after_a_manual_edit_is_409_proposal_stale(api_client, seeded, offline):
    user, client = seeded.user, api_client
    protocol = await make_protocol(client, user, seeded.drug)
    pid = protocol["id"]
    sid = await edit_session(client, user, pid)
    impact, diff = await prose_proposal_ready(client, user, sid)
    proposal_id = impact["data"]["proposal_id"]

    await manual_edit(client, user, pid)  # version 1 -> 2
    resp = await click(client, user, sid, diff, "apply_edit", {"proposal_id": proposal_id})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "proposal_stale"

    stored = await reload(seeded.db, Protocol, pid)
    assert stored.version == 2 and stored.sections["synopsis"] == "<p>Edited by hand.</p>"
    assert "[MOCK REVISION]" not in stored.sections["safety_monitoring"]
    assert (await reload(seeded.db, ProtocolEditProposal, proposal_id)).status.value == "stale"
    changes = (await seeded.db.execute(select(ProtocolVersion).where(ProtocolVersion.change_type == "CHAT_EDIT"))).scalars().all()
    assert changes == []
    polled = await client.get(f"/api/chat/{sid}/proposals/{proposal_id}", headers=as_user(user))
    assert polled.json()["status"] == "stale" and polled.json()["diff"] == []
    # The dead diff card can no longer be clicked.
    detail = await get_chat(client, user, sid)
    card = next(x for x in detail["cards"] if x["id"] == diff["id"])
    assert card["status"] == "superseded" and card["actions"] == []
    # And asking again starts from the latest text.
    impact2 = await request_prose_edit(client, user, sid)
    assert impact2["data"]["base_version"] == 2


async def test_a_ready_proposal_goes_stale_when_the_chat_is_read_after_a_manual_edit(api_client, seeded, offline):
    user, client = seeded.user, api_client
    protocol = await make_protocol(client, user, seeded.drug)
    pid = protocol["id"]
    sid = await edit_session(client, user, pid)
    impact, diff = await prose_proposal_ready(client, user, sid)
    await manual_edit(client, user, pid)
    detail = await get_chat(client, user, sid)
    card = next(x for x in detail["cards"] if x["id"] == diff["id"])
    assert card["status"] == "superseded" and card["data"]["stale"] is True
    assert (await reload(seeded.db, ProtocolEditProposal, impact["data"]["proposal_id"])).status.value == "stale"


async def test_confirming_after_the_protocol_moved_on_is_409_and_starts_nothing(api_client, seeded, offline):
    user, client = seeded.user, api_client
    protocol = await make_protocol(client, user, seeded.drug)
    pid = protocol["id"]
    sid = await edit_session(client, user, pid)
    impact = await request_prose_edit(client, user, sid)
    await manual_edit(client, user, pid)
    resp = await confirm(client, user, sid, impact)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "proposal_stale"
    assert (await seeded.db.execute(select(ProtocolEditProposal))).scalars().all() == []


async def test_approved_protocols_are_blocked_everywhere(api_client, seeded, offline):
    user, client = seeded.user, api_client
    protocol = await make_protocol(client, user, seeded.drug)
    pid = protocol["id"]
    sid = await edit_session(client, user, pid)
    impact, diff = await prose_proposal_ready(client, user, sid)
    proposal_id = impact["data"]["proposal_id"]
    impact_b = await request_prose_edit(client, user, sid, "rewrite the synopsis")

    stored = await reload(seeded.db, Protocol, pid)
    stored.status = ProtocolStatus.approved
    await seeded.db.commit()

    # 1. A new edit chat on an approved protocol is refused up front.
    refused = await client.post("/api/chat", json={"kind": "protocol_edit", "protocol_id": pid}, headers=as_user(user))
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "invalid_action"
    assert "draft" in refused.json()["detail"]["message"]

    # 2. Confirming a pending request is refused; applying a ready proposal is stale; nothing changes.
    resp = await confirm(client, user, sid, impact_b)
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "invalid_action"
    resp = await click(client, user, sid, diff, "apply_edit", {"proposal_id": proposal_id})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "proposal_stale"
    stored = await reload(seeded.db, Protocol, pid)
    assert stored.version == 1 and "[MOCK REVISION]" not in stored.sections["safety_monitoring"]

    # 3. The assistant can no longer even propose: the overview says it is not editable.
    known = {x["id"] for x in (await get_chat(client, user, sid))["cards"]}
    detail = await propose(client, user, sid, "shorten the adverse events section and give me an overview")
    reply = text_of_last_assistant(detail).lower()
    assert "draft" in reply or "cannot be edited" in reply
    fresh = [x for x in detail["cards"] if x["id"] not in known]
    assert not any(x["type"] == "edit_impact" for x in fresh)  # no new request can even be recorded
    overview = next(x for x in fresh if x["type"] == "protocol_overview")
    assert overview["data"]["editable"] is False and "draft" in overview["data"]["blocked_reason"]


# ---------------------------------------------------------------------------
# Scoping, failures, the production (background) path
# ---------------------------------------------------------------------------


async def test_a_proposal_cannot_be_applied_from_another_chat(api_client, seeded, offline):
    user, client = seeded.user, api_client
    protocol = await make_protocol(client, user, seeded.drug)
    pid = protocol["id"]
    sid_a = await edit_session(client, user, pid)
    sid_b = await edit_session(client, user, pid)
    impact, diff = await prose_proposal_ready(client, user, sid_a)
    proposal_id = impact["data"]["proposal_id"]

    resp = await click(client, user, sid_b, {"id": str(uuid.uuid4())}, "apply_edit", {"proposal_id": proposal_id})
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "not_found"
    polled = await client.get(f"/api/chat/{sid_b}/proposals/{proposal_id}", headers=as_user(user))
    assert polled.status_code == 404
    assert (await reload(seeded.db, Protocol, pid)).version == 1


async def test_a_failing_job_marks_the_proposal_failed_and_the_card_says_so(api_client, seeded, offline, monkeypatch):
    from services.ai_service import AIService

    async def boom(self, *a, **k):
        raise RuntimeError("model exploded")

    user, client = seeded.user, api_client
    protocol = await make_protocol(client, user, seeded.drug)  # generated before the model is broken
    monkeypatch.setattr(AIService, "generate_protocol_section", boom)
    sid = await edit_session(client, user, protocol["id"])
    impact = await request_prose_edit(client, user, sid)
    resp = await confirm(client, user, sid, impact)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    progress = card_of(body, "edit_progress")
    assert progress["status"] == "error" and progress["data"]["status"] == "failed"
    assert "could not be prepared" in body["guide"]["text"]
    assert not any(x["type"] == "edit_diff" for x in body["cards"])
    row = await reload(seeded.db, ProtocolEditProposal, impact["data"]["proposal_id"])
    assert row.status.value == "failed" and "model exploded" not in (row.error or "")
    retry = await click(client, user, sid, {"id": str(uuid.uuid4())}, "apply_edit", {"proposal_id": str(row.id)})
    assert retry.status_code == 422


async def test_a_pipeline_http_error_becomes_the_failure_reason(api_client, seeded, offline, monkeypatch):
    from fastapi import HTTPException

    from routers import protocols as router

    user, client = seeded.user, api_client
    await add_pk(seeded.db, seeded.drug, 24.0)
    protocol = await make_protocol(client, user, seeded.drug)  # generated with the real (mock-AI) pipeline
    sid = await edit_session(client, user, protocol["id"])
    detail = await propose(client, user, sid, "make the washout 14 days")
    impact = active(detail["cards"], "edit_impact")
    assert impact

    async def refuse(*a, **k):
        raise HTTPException(422, detail="A washout of 3 days is shorter than the minimum of 7 days.")

    monkeypatch.setattr(router, "_run_generation_pipeline", refuse)  # the re-run is what fails
    resp = await confirm(client, user, sid, impact)
    assert resp.status_code == 200, resp.text
    progress = card_of(resp.json(), "edit_progress")
    assert progress["data"]["status"] == "failed" and "shorter than the minimum" in progress["data"]["error"]
    assert (await reload(seeded.db, Protocol, protocol["id"])).version == 1


async def _async_mode(monkeypatch):
    """The production path: the job is spawned, not run inline. Returns the list
    the spawned proposal ids are appended to (the test runs them by hand)."""
    from routers import protocols as router

    spawned: list[str] = []

    async def capture(proposal_id: str) -> None:
        spawned.append(proposal_id)

    real = edits.run_proposal_job
    monkeypatch.setattr(router, "_generate_synchronously", lambda: False)
    monkeypatch.setattr(edits, "run_proposal_job", capture)
    return spawned, real


async def test_production_path_the_card_follows_the_proposal_status(api_client, seeded, offline, monkeypatch):
    user, client = seeded.user, api_client
    protocol = await make_protocol(client, user, seeded.drug)
    pid = protocol["id"]
    spawned, run_job = await _async_mode(monkeypatch)

    sid = await edit_session(client, user, pid)
    impact = await request_prose_edit(client, user, sid)
    resp = await confirm(client, user, sid, impact)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    progress = card_of(body, "edit_progress")
    assert progress["status"] == "active" and progress["data"]["status"] == "generating"
    assert not any(x["type"] == "edit_diff" for x in body["cards"])
    assert body["guide"]["text"]
    proposal_id = impact["data"]["proposal_id"]
    assert spawned == [proposal_id]

    # Still generating: polling shows it, the transcript is unchanged.
    polled = await client.get(f"/api/chat/{sid}/proposals/{proposal_id}", headers=as_user(user))
    assert polled.json()["status"] == "generating" and polled.json()["diff"] == []
    detail = await get_chat(client, user, sid)
    assert not any(x["type"] == "edit_diff" for x in detail["cards"])

    # The job finishes: the next read flips the progress card and adds the diff card, once.
    await run_job(proposal_id)
    polled = await client.get(f"/api/chat/{sid}/proposals/{proposal_id}", headers=as_user(user))
    assert polled.json()["status"] == "ready" and [d["section_name"] for d in polled.json()["diff"]] == ["safety_monitoring"]
    detail = await get_chat(client, user, sid)
    by_type = {}
    for x in detail["cards"]:
        by_type.setdefault(x["type"], []).append(x)
    assert by_type["edit_progress"][0]["status"] == "done"
    assert len(by_type["edit_diff"]) == 1 and by_type["edit_diff"][0]["data"]["status"] == "ready"
    placed = {cid for m in detail["messages"] for cid in m["card_ids"]}
    assert by_type["edit_diff"][0]["id"] in placed
    detail2 = await get_chat(client, user, sid)
    assert sum(1 for x in detail2["cards"] if x["type"] == "edit_diff") == 1

    # ...and it applies like any other.
    resp = await click(client, user, sid, by_type["edit_diff"][0], "apply_edit", {"proposal_id": proposal_id})
    assert resp.status_code == 200, resp.text
    assert (await reload(seeded.db, Protocol, pid)).version == 2


async def test_discarding_while_generating_drops_the_late_result(api_client, seeded, offline, monkeypatch):
    user, client = seeded.user, api_client
    protocol = await make_protocol(client, user, seeded.drug)
    spawned, run_job = await _async_mode(monkeypatch)
    sid = await edit_session(client, user, protocol["id"])
    impact = await request_prose_edit(client, user, sid)
    await confirm(client, user, sid, impact)
    proposal_id = spawned[0]

    resp = await click(client, user, sid, {"id": str(uuid.uuid4())}, "discard_edit", {"proposal_id": proposal_id})
    assert resp.status_code == 200, resp.text
    await run_job(proposal_id)  # the job was already in flight
    row = await reload(seeded.db, ProtocolEditProposal, proposal_id)
    assert row.status.value == "discarded" and row.proposed_sections is None


async def test_a_proposal_stuck_generating_for_over_30_minutes_is_failed_on_read(api_client, seeded, offline, monkeypatch):
    user, client = seeded.user, api_client
    protocol = await make_protocol(client, user, seeded.drug)
    spawned, _run_job = await _async_mode(monkeypatch)
    sid = await edit_session(client, user, protocol["id"])
    impact = await request_prose_edit(client, user, sid)
    await confirm(client, user, sid, impact)
    proposal_id = spawned[0]

    # 29 minutes: still going.
    row = await reload(seeded.db, ProtocolEditProposal, proposal_id)
    row.started_at = datetime.now(timezone.utc) - timedelta(minutes=29)
    await seeded.db.commit()
    assert (await client.get(f"/api/chat/{sid}/proposals/{proposal_id}", headers=as_user(user))).json()["status"] == "generating"

    row = await reload(seeded.db, ProtocolEditProposal, proposal_id)
    row.started_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    await seeded.db.commit()
    polled = await client.get(f"/api/chat/{sid}/proposals/{proposal_id}", headers=as_user(user))
    assert polled.json()["status"] == "failed" and "took too long" in polled.json()["error"]
    detail = await get_chat(client, user, sid)
    progress = next(x for x in detail["cards"] if x["type"] == "edit_progress")
    assert progress["status"] == "error" and progress["data"]["status"] == "failed"
    # A restart-orphaned job that wakes up late must not resurrect it.
    await edits.run_proposal_job(proposal_id)
    assert (await reload(seeded.db, ProtocolEditProposal, proposal_id)).status.value == "failed"


# ---------------------------------------------------------------------------
# Pure: merging an engine edit into the stored inputs
# ---------------------------------------------------------------------------

STORED = {
    "dose_strength": "850 mg", "subject_type": "healthy", "exclusion_presets": ["Tobacco use"],
    "washout_days": 14.0, "cv_intra": 30.0,
    "_randomization": {"allocation": [1, 2, 3]}, "_randomization_seed": 12345,
    "_sample_size": {"design_key": "crossover", "t_half_hours": 24.0},
    "_design_decisions": [
        {"key": "population", "label": "Population", "value": "Healthy volunteers", "options": ["Healthy volunteers", "Patients"]},
        {"key": "design", "label": "Design", "value": "Crossover", "options": ["Crossover", "Parallel"]},
    ],
}


def test_merge_carries_inputs_and_the_seed_but_never_the_allocation_or_engine_outputs():
    merged = edits.merge_engine_changes(STORED, {"cv_intra": 45.0}, guideline_version="FDA-BE-2014")
    cfg = merged.population_config
    assert cfg["cv_intra"] == 45.0 and cfg["dose_strength"] == "850 mg" and cfg["washout_days"] == 14.0
    assert cfg["_randomization_seed"] == 12345
    assert "_randomization" not in cfg and "_sample_size" not in cfg
    assert [d["key"] for d in merged.design_decisions] == ["population", "design"]
    assert STORED["cv_intra"] == 30.0  # the stored config is never mutated
    assert merged.guideline_version == "FDA-BE-2014" and merged.flips_to_parallel is False


def test_merge_exclusions_clear_and_guideline_changes():
    merged = edits.merge_engine_changes(
        STORED, {"exclusion_add": "Recent surgery", "guideline_version": "EMA-BE-2010", "cv_intra": None},
        guideline_version="FDA-BE-2014",
    )
    assert merged.population_config["exclusion_presets"] == ["Tobacco use", "Recent surgery"]
    assert "cv_intra" not in merged.population_config
    assert merged.guideline_version == "EMA-BE-2010"
    removed = edits.merge_engine_changes(STORED, {"exclusion_remove": "tobacco USE"}, guideline_version="x")
    assert removed.population_config["exclusion_presets"] == []
    dup = edits.merge_engine_changes(STORED, {"exclusion_add": "TOBACCO use"}, guideline_version="x")
    assert dup.population_config["exclusion_presets"] == ["Tobacco use"]


def test_merge_a_flip_to_parallel_drops_the_washout():
    merged = edits.merge_engine_changes(STORED, {"design_decision_design": "Parallel"}, guideline_version="x")
    assert merged.flips_to_parallel is True
    assert "washout_days" not in merged.population_config
    assert next(d for d in merged.design_decisions if d["key"] == "design")["value"] == "Parallel"
    assert any("washout" in n for n in merged.notes)
    back = edits.merge_engine_changes(STORED, {"design_decision_design": "Crossover"}, guideline_version="x")
    assert back.flips_to_crossover is True and back.population_config["washout_days"] == 14.0


def test_merge_subject_type_also_aligns_the_population_decision():
    merged = edits.merge_engine_changes(STORED, {"subject_type": "patient"}, guideline_version="x")
    assert merged.population_config["subject_type"] == "patient"
    assert next(d for d in merged.design_decisions if d["key"] == "population")["value"] == "Patients"
    assert merged.notes
    with pytest.raises(ValueError):
        edits.merge_engine_changes(STORED, {"design_decision_pd_endpoint": "x"}, guideline_version="x")


def test_fact_snapshot_never_exposes_randomization_or_unlisted_config():
    cfg = {
        **STORED,
        "_sample_size": {"design_key": "crossover", "n_enrolled": 48, "washout_days": 7.0, "secret": "nope"},
        "_ddi": {"prohibited": [1, 2], "restricted": [], "permitted": [1]},
    }
    snap = edits.fact_snapshot(cfg, "FDA-BE-2014", "BE")
    assert "secret" not in snap["facts"] and snap["facts"]["ddi_prohibited_rows"] == 2
    assert "randomization" not in json.dumps(snap).lower()
    rows = {r["label"]: r["value"] for r in edits.fact_rows(cfg, "FDA-BE-2014", "BE")}
    assert rows["Washout between periods"] == "7 days" and rows["Planned enrolment"] == "48"


def test_validate_engine_request_uses_the_stored_design_and_half_life():
    protocol = type("P", (), {})()
    protocol.population_config = STORED
    protocol.guideline_version = "FDA-BE-2014"
    edits.validate_engine_request(protocol, {"changes": {"washout_days": 7.0}})  # = the floor
    with pytest.raises(edits.ProposalError) as short:
        edits.validate_engine_request(protocol, {"changes": {"washout_days": 5.0}})
    assert short.value.status_code == 422 and "7 days" in short.value.message
    with pytest.raises(edits.ProposalError) as flip:
        edits.validate_engine_request(protocol, {"changes": {"washout_days": 14.0, "design_decision_design": "Parallel"}})
    assert "parallel" in flip.value.message.lower()
    edits.validate_engine_request(protocol, {"changes": {"cv_intra": 40.0}})  # no washout: nothing to check
