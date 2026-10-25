"""Budget chat (B4): a budget conversation ends in ``create_budget``, and the
total the ready card shows is exactly what the budgets API returns for the new
budget (both come from the same server-side rollup).

No credits and no network: MOCK_AI scripted model, the ClinicalTrials.gov budget
precedent lookup stubbed (``no_real_ctgov_calls_for_budgets``).
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from models import AuditLog, Budget, ChatSession, UserRole
from tests.test_chat_api import (  # noqa: F401  (fixtures + helpers)
    active,
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
from tests.test_chat_auth import add_user

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("no_real_ctgov_calls_for_budgets")]


async def budget_from_api(client, user, budget_id: str) -> dict:
    resp = await client.get(f"/api/budgets/{budget_id}", headers=as_user(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_patient_budget_chat_ends_in_create_budget_and_the_total_matches_the_api(api_client, seeded):
    user, client = seeded.user, api_client
    made = await create_chat(client, user, kind="budget")
    sid = made["id"]
    assert made["kind"] == "budget"

    # 1. "Price a patient study for nivolumab" -> the estimate card.
    turn = await send_turn(client, user, sid, "Price a patient study for nivolumab")
    events = parse_sse(turn.text)
    assert names(events)[-1] == "turn_end" and "error" not in names(events)
    detail = await get_chat(client, user, sid)
    preview = active(detail["cards"], "budget_preview")
    assert preview, [x["type"] for x in detail["cards"]]
    assert preview["data"]["drug_inn"] == "nivolumab" and preview["data"]["subject_profile"] == "patient"
    assert preview["data"]["estimated_grand_total"] > 0
    fields = {f["path"]: f for f in detail["spec"]["fields"]}
    assert fields["drug_inn"]["display_value"] == "nivolumab"

    # 2. "use 40 sites": confirmed through the quote rule (the user's own words).
    turn = await send_turn(client, user, sid, "use 40 sites")
    assert names(parse_sse(turn.text))[-1] == "turn_end"
    fields = {f["path"]: f for f in (await get_chat(client, user, sid))["spec"]["fields"]}
    assert fields["n_sites_selected"]["status"] == "confirmed"
    assert fields["n_sites_selected"]["source"] == "user_text"
    assert "40" in fields["n_sites_selected"]["display_value"]

    # 3. The summary card carries the Create action.
    turn = await send_turn(client, user, sid, "show me the summary")
    assert names(parse_sse(turn.text))[-1] == "turn_end"
    detail = await get_chat(client, user, sid)
    summary = active(detail["cards"], "budget_summary")
    assert summary and summary["data"]["ready"] is True
    assert [a["id"] for a in summary["actions"]] == ["create_budget"]

    # 4. Create: a user click, no model.
    resp = await click(client, user, sid, summary, "create_budget")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ready = next(x for x in body["cards"] if x["type"] == "budget_ready")
    assert ready["status"] == "done"
    budget_id = ready["data"]["budget_id"]
    assert ready["data"]["drug_inn"] == "nivolumab" and ready["data"]["n_enrolled"]
    assert any(x["id"] == summary["id"] and x["status"] == "done" for x in body["cards"])
    assert_no_randomization(resp.text)

    # 5. The total on the card IS the total the budgets API returns.
    api = await budget_from_api(client, user, budget_id)
    assert ready["data"]["grand_total"] == api["rollup"]["grand_total"]
    assert ready["data"]["category_count"] == len(api["categories"])
    assert api["org_id"] == user.org_id and api["drug_inn"] == "nivolumab"
    listed = await client.get("/api/budgets", headers=as_user(user))
    assert budget_id in [b["id"] for b in listed.json()]

    # 6. The session is linked to the budget, the draft persisted, an audit row names the chat.
    detail = await get_chat(client, user, sid)
    assert detail["session"]["budget_id"] == budget_id
    assert {x["type"] for x in detail["cards"]} >= {"budget_preview", "budget_summary", "budget_ready"}
    placed = {cid for m in detail["messages"] for cid in m["card_ids"]}
    assert ready["id"] in placed
    audit = (await seeded.db.execute(select(AuditLog).where(AuditLog.action == "CHAT_CREATE:budget"))).scalars().all()
    assert audit and audit[0].resource_id == budget_id and audit[0].diff["chat_session_id"] == sid
    assert audit[0].diff["source"] == "chat"
    assert_no_randomization(json.dumps(detail))


async def test_the_preview_estimate_equals_the_created_total_when_nothing_else_changes(api_client, seeded):
    """A drug-name-only budget: the card's estimate and the created budget's
    rollup come from the same engine, so they agree to the cent."""
    user, client = seeded.user, api_client
    sid = (await create_chat(client, user, kind="budget"))["id"]
    await send_turn(client, user, sid, "Price a bioequivalence study for metformin")
    detail = await get_chat(client, user, sid)
    preview = active(detail["cards"], "budget_preview")
    assert preview
    turn = await send_turn(client, user, sid, "summary please")
    assert names(parse_sse(turn.text))[-1] == "turn_end"
    summary = active((await get_chat(client, user, sid))["cards"], "budget_summary")
    resp = await click(client, user, sid, summary, "create_budget")
    assert resp.status_code == 200, resp.text
    ready = next(x for x in resp.json()["cards"] if x["type"] == "budget_ready")
    api = await budget_from_api(client, user, ready["data"]["budget_id"])
    assert api["rollup"]["grand_total"] == ready["data"]["grand_total"]
    assert round(preview["data"]["estimated_grand_total"], 2) == round(api["rollup"]["grand_total"], 2)


async def test_budget_chat_with_a_protocol_links_it_and_prices_from_its_schedule(api_client, seeded, offline):
    """'Price this study' starts a budget chat with protocol_id: the draft is linked
    from the start and the created budget carries the link."""
    user, client = seeded.user, api_client
    created = await client.post(
        "/api/protocols/",
        json={"drug_id": str(seeded.drug.id), "study_type": "BE", "population_config": {"dose_strength": "850 mg"}},
        headers=as_user(user),
    )
    assert created.status_code == 201, created.text
    protocol_id = created.json()["id"]

    made = await create_chat(client, user, kind="budget", protocol_id=protocol_id)
    sid = made["id"]
    assert made["protocol_id"] == protocol_id
    detail = await get_chat(client, user, sid)
    fields = {f["path"]: f for f in detail["spec"]["fields"]}
    assert fields["protocol_id"]["display_value"] and fields["protocol_id"]["source"] == "user_click"

    turn = await send_turn(client, user, sid, "give me the summary")
    assert names(parse_sse(turn.text))[-1] == "turn_end"
    summary = active((await get_chat(client, user, sid))["cards"], "budget_summary")
    assert summary["data"]["linked_protocol"]["id"] == protocol_id and summary["data"]["ready"]
    resp = await click(client, user, sid, summary, "create_budget")
    assert resp.status_code == 200, resp.text
    ready = next(x for x in resp.json()["cards"] if x["type"] == "budget_ready")
    assert ready["data"]["protocol_id"] == protocol_id
    assert (await budget_from_api(client, user, ready["data"]["budget_id"]))["protocol_id"] == protocol_id


async def test_linking_a_protocol_from_the_picker_card(api_client, seeded, offline):
    user, client = seeded.user, api_client
    created = await client.post(
        "/api/protocols/",
        json={"drug_id": str(seeded.drug.id), "study_type": "BE", "population_config": {"dose_strength": "850 mg"}},
        headers=as_user(user),
    )
    protocol_id = created.json()["id"]
    sid = (await create_chat(client, user, kind="budget"))["id"]
    await send_turn(client, user, sid, "Price a study for metformin and link my protocols")
    detail = await get_chat(client, user, sid)
    picker = active(detail["cards"], "budget_summary")
    assert picker and [p["id"] for p in picker["data"]["protocol_options"]] == [protocol_id]

    resp = await click(client, user, sid, picker, "link_protocol", {"protocol_id": protocol_id})
    assert resp.status_code == 200, resp.text
    # Linking something that is not in the org is refused.
    bad = await click(client, user, sid, picker, "link_protocol", {"protocol_id": str(uuid.uuid4())})
    assert bad.status_code == 404
    other = await add_user(seeded.db, "other@elsewhere.example", org_id="other-org")
    other_sid = (await create_chat(client, other, kind="budget"))["id"]
    foreign = await click(client, other, other_sid, {"id": str(uuid.uuid4())}, "link_protocol", {"protocol_id": protocol_id}, spec_version=0)
    assert foreign.status_code == 403  # another org's protocol: same 403 as every protocol route
    fields = {f["path"]: f for f in (await get_chat(client, other, other_sid))["spec"]["fields"]}
    assert not fields["protocol_id"]["display_value"]


async def test_the_biologic_study_type_gate_applies_to_budgets_too(api_client, seeded):
    user, client = seeded.user, api_client
    sid = (await create_chat(client, user, kind="budget"))["id"]
    resp = await click(
        client, user, sid, {"id": str(uuid.uuid4())}, "set_budget_fields",
        {"patch": {"drug_inn": "adalimumab", "study_type": "BE"}}, spec_version=0,
    )
    assert resp.status_code == 422, resp.text
    assert "biologic" in resp.json()["detail"]["message"]
    assert (await seeded.db.execute(select(Budget))).scalars().all() == []


async def test_creating_without_a_drug_is_refused_and_nothing_is_created(api_client, seeded):
    user, client = seeded.user, api_client
    sid = (await create_chat(client, user, kind="budget"))["id"]
    resp = await click(client, user, sid, {"id": str(uuid.uuid4())}, "create_budget", spec_version=0)
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "invalid_action"
    assert (await seeded.db.execute(select(Budget))).scalars().all() == []


async def test_a_reviewer_can_run_a_budget_chat_and_create_a_budget(api_client, seeded):
    """Budgets are not signed regulatory artifacts, so there is no role gate."""
    reviewer = await add_user(seeded.db, "reviewer@testplatform.com", UserRole.reviewer)
    client = api_client
    sid = (await create_chat(client, reviewer, kind="budget"))["id"]
    turn = await send_turn(client, reviewer, sid, "Price a bioequivalence study for metformin")
    assert turn.status_code == 200 and "error" not in names(parse_sse(turn.text))
    await send_turn(client, reviewer, sid, "summary please")
    summary = active((await get_chat(client, reviewer, sid))["cards"], "budget_summary")
    resp = await click(client, reviewer, sid, summary, "create_budget")
    assert resp.status_code == 200, resp.text
    row = await seeded.db.get(ChatSession, uuid.UUID(sid))
    await seeded.db.refresh(row)
    assert row.budget_id is not None
