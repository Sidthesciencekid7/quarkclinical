"""Pure tests for services/chat/mock_model.py: the MOCK_AI scripted model, and the
whole chat flow walked through it with an in-memory repository and fake engine
facades (zero credits, no database)."""
from __future__ import annotations

import asyncio
import json
import re
import uuid

import pytest

from services.chat import cards as K
from services.chat import contract as c
from services.chat import mock_model as M
from services.chat import orchestrator as O
from services.chat import store as S
from services.chat import tools as T
from services.chat.contract import CardStatus, CardType, FieldStatus, MessageRole, SessionKind
from services.chat.facades import StaticFacades
from services.chat.spec import new_spec

USER = "11111111-1111-4111-8111-111111111111"
ORG = "org-1"
ADA = "00000000-0000-4000-8000-000000000001"
IBU = "00000000-0000-4000-8000-000000000003"
PROTO_ID = "99999999-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("MOCK_AI", "CHAT_MODEL", "LLM_PROVIDER", "CHAT_EFFORT", "CHAT_MAX_TOOL_ITERATIONS"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Building requests the way the orchestrator does
# ---------------------------------------------------------------------------


def request_for(kind: str, user_text: str, spec_body: dict | None = None, tool_results: list | None = None, prior_tool_use: list | None = None):
    tools = T.tool_definitions(kind)
    spec = {"ready": False, "next_step": "drug", "missing": [], "fields": []}
    spec.update(spec_body or {})
    messages: list[dict] = [
        {"role": "user", "content": [{"type": "text", "text": user_text}]},
        {"role": "system", "content": '<draft_spec schema="x" kind="y" version="0">\n' + json.dumps(spec) + "\n</draft_spec>"},
    ]
    if prior_tool_use:
        messages.append({"role": "assistant", "content": prior_tool_use})
        messages.append({"role": "user", "content": tool_results or []})
    return {"model": "claude-opus-5-5", "tools": tools, "messages": messages}


def plan(kind: str, text: str, **kw):
    blocks = M.script_response(request_for(kind, text, **kw))
    return [(b.name, b.input) for b in blocks if b.type == "tool_use"]


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------


def test_protocol_patterns_emit_the_expected_tools():
    calls = dict(plan("protocol", "Biosimilar PK study for adalimumab in healthy volunteers, age 18-45"))
    assert calls["search_drugs"] == {"query": "adalimumab"}
    changes = {ch["field"]: ch for ch in calls["update_draft"]["changes"]}
    assert changes["study_type"]["value"] == "biosimilar_pk" and changes["subject_type"]["value"] == "healthy"
    assert (changes["age_min"]["value"], changes["age_max"]["value"]) == ("18", "45")


def test_more_protocol_patterns():
    ch = lambda text: {x["field"]: x for x in dict(plan("protocol", text)).get("update_draft", {"changes": []})["changes"]}  # noqa: E731
    assert ch("a bioequivalence study of metformin")["study_type"]["value"] == "BE"
    assert ch("a BE study")["study_type"]["value"] == "BE"
    assert ch("food effect study")["study_type"]["value"] == "food_effect"
    assert ch("in patients")["subject_type"]["value"] == "patient"
    assert ch("men only")["sex"]["value"] == "male"
    assert ch("use EMA")["guideline_version"]["value"] == "EMA-BE-2010"
    assert ch("use FDA")["guideline_version"]["value"] == "FDA-BE-2014"
    assert ch("ICH M13A please")["guideline_version"]["value"] == "ICH-M13A-2024"
    assert ch("assume a CV of 30%")["cv_intra"]["value"] == "30"


def test_other_protocol_tools_follow_keywords():
    assert [n for n, _ in plan("protocol", "please generate it")] == ["show_review_summary"]
    assert [n for n, _ in plan("protocol", "show me the interactions")] == ["screen_interactions"]
    assert [n for n, _ in plan("protocol", "review the design")] == ["preview_design"]
    assert [n for n, _ in plan("protocol", "what indications are possible")] == ["rank_indications"]
    assert [n for n, _ in plan("protocol", "which guidelines can I pick")] == ["list_guidelines"]
    assert plan("protocol", "why is the washout that long?") == [("explain_rule", {"topic": "washout"})]
    assert plan("protocol", "how is the sample size worked out?") == [("explain_rule", {"topic": "sample_size"})]


def test_budget_patterns():
    calls = dict(plan("budget", "Price a patient study for nivolumab with 40 sites in 12 countries, EUR"))
    changes = {x["field"]: x for x in calls["update_budget_draft"]["changes"]}
    assert changes["drug_inn"]["value"] == "nivolumab" and changes["subject_profile"]["value"] == "patient"
    assert changes["n_sites_selected"]["value"] == "40" and changes["n_countries"]["value"] == "12"
    assert changes["currency"]["value"] == "EUR"
    assert calls["preview_budget"] == {"drug_inn": "nivolumab"}
    assert [n for n, _ in plan("budget", "link one of my protocols")] == ["list_my_protocols"]
    assert [n for n, _ in plan("budget", "show the summary")] == ["show_budget_summary"]


def test_edit_patterns():
    assert [n for n, _ in plan("protocol_edit", "hello", spec_body={"next_step": "overview"})] == ["get_protocol_overview"]
    calls = dict(plan("protocol_edit", "Please shorten the adverse events section", spec_body={"next_step": "request"}))
    assert calls["propose_section_revision"]["sections"] == ["safety_monitoring"]
    assert calls["propose_section_revision"]["user_quote"] in "Please shorten the adverse events section"
    calls = dict(plan("protocol_edit", "make the washout 14 days", spec_body={"next_step": "request"}))
    assert calls["propose_input_change"]["changes"] == [{"field": "washout_days", "value": "14"}]
    calls = dict(plan("protocol_edit", "set the CV to 35%", spec_body={"next_step": "request"}))
    assert calls["propose_input_change"]["changes"] == [{"field": "cv_intra", "value": "35"}]
    assert [n for n, _ in plan("protocol_edit", "run the qc", spec_body={"next_step": "request"})] == ["get_qc_summary"]


@pytest.mark.parametrize("kind,text", [
    ("protocol", "Biosimilar PK study for adalimumab in HEALTHY volunteers, Age 18 to 45, MEN only, use EMA, CV of 28.5%"),
    ("protocol", "a bioequivalence study of metformin in patients"),
    ("budget", "Price a PATIENT study for Nivolumab with 40 Sites and 8 countries"),
    ("protocol_edit", "Could you rewrite the Introduction? Also set CV to 22 and washout of 10 days"),
])
def test_every_value_the_mock_sets_is_quoted_verbatim_from_the_users_message(kind, text):
    for name, args in plan(kind, text, spec_body={"next_step": "request"}):
        quotes = [args["user_quote"]] if "user_quote" in args else [ch["user_quote"] for ch in args.get("changes", []) if "user_quote" in ch]
        for q in quotes:
            assert q and q.lower() in text.lower(), (name, q)


def test_nothing_matched_gives_a_plain_reply_for_the_next_step():
    blocks = M.script_response(request_for("protocol", "hmm", spec_body={"next_step": "study_type"}))
    assert [b.type for b in blocks] == ["text"] and "type of study" in blocks[0].text


def test_the_mock_writes_no_thinking_blocks_and_no_numbers_of_its_own():
    for kind, text in [("protocol", "Biosimilar PK study for adalimumab"), ("budget", "Price nivolumab"), ("protocol", "generate")]:
        blocks = M.script_response(request_for(kind, text))
        assert all(b.type in ("text", "tool_use") for b in blocks)
    reply = M.script_response(request_for(
        "protocol", "x", prior_tool_use=[{"type": "tool_use", "id": "t1", "name": "show_review_summary", "input": {}}],
        tool_results=[{"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"ready": False, "missing": ["Drug"]})}],
    ))
    assert not re.search(r"\d", reply[0].text)


def test_continuation_reply_reflects_the_tool_results():
    def reply_to(name, result, is_error=False):
        block = {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(result)}
        if is_error:
            block["is_error"] = True
        return M.script_response(request_for("protocol", "x", prior_tool_use=[{"type": "tool_use", "id": "t1", "name": name, "input": {}}], tool_results=[block]))[0].text

    assert "Pick the right one" in reply_to("search_drugs", {"matches": [{"id": "1"}]})
    assert "could not find" in reply_to("search_drugs", {"matches": []})
    assert "'adalimumab' is a biologic" in reply_to("update_draft", {"results": [{"field": "study_type", "accepted": False, "message": "'adalimumab' is a biologic and cannot run a 'BE' study; allowed study type(s): biosimilar_pk."}]})
    assert "Not ready yet. Still needed: Drug." in reply_to("show_review_summary", {"ready": False, "missing": ["Drug"]})
    assert "Generate protocol" in reply_to("show_review_summary", {"ready": True})
    assert reply_to("search_drugs", {"error": "database down"}, is_error=True) == "database down"
    assert reply_to("explain_rule", {"title": "The washout rule", "text": "Seven half-lives."}).startswith("The washout rule: Seven")


# ---------------------------------------------------------------------------
# The streaming surface the orchestrator uses
# ---------------------------------------------------------------------------


async def collect(stream):
    events = []
    async with stream as s:
        async for e in s:
            events.append(e)
        final = await s.get_final_message()
    return events, final


async def test_stream_events_and_final_message_shape():
    client = M.MockAnthropicClient()
    events, final = await collect(client.messages.stream(**request_for("protocol", "Biosimilar study of adalimumab")))
    kinds = [e.type for e in events]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop" and "message_delta" in kinds
    text_deltas = [e.delta.text for e in events if e.type == "content_block_delta" and e.delta.type == "text_delta"]
    assert "".join(text_deltas) == final.content[0].text
    assert final.stop_reason == "tool_use" and final.model == "claude-opus-5-5"
    assert final.usage.input_tokens == 0 and final._request_id.startswith("req_mock")
    tool_blocks = [b for b in final.content if b.type == "tool_use"]
    assert tool_blocks and tool_blocks[0].id.startswith("toolu_mock_")
    dumped = tool_blocks[0].model_dump(mode="json", exclude_none=True)
    assert set(dumped) == {"type", "id", "name", "input"}
    # both namespaces (the orchestrator picks beta when server-side fallbacks apply) and extra params are accepted
    events2, final2 = await collect(client.beta.messages.stream(**request_for("budget", "hello"), betas=["x"], fallbacks="default", cache_control={"type": "ephemeral"}))
    assert final2.stop_reason == "end_turn" and len(client.calls) == 2


async def test_tool_use_ids_are_stable_for_the_same_request():
    a = M.MockAnthropicClient().respond(request_for("protocol", "adalimumab"))
    b = M.MockAnthropicClient().respond(request_for("protocol", "adalimumab"))
    assert [x.id for x in a.content if x.type == "tool_use"] == [x.id for x in b.content if x.type == "tool_use"]


# ---------------------------------------------------------------------------
# Walking the whole flow with MOCK_AI (orchestrator + repository + fake facades)
# ---------------------------------------------------------------------------


class Walk:
    def __init__(self, kind: str, facades=None, **spec_init):
        self.repo = S.InMemoryRepository()
        self.facades = facades or StaticFacades()
        self.kind = kind
        self.spec_init = spec_init
        self.session: S.SessionRecord | None = None

    async def start(self):
        self.session = await self.repo.create_session(
            org_id=ORG, created_by=USER, kind=SessionKind(self.kind), spec=new_spec(self.kind, **self.spec_init).to_json(),
        )
        return self

    async def say(self, text: str):
        q: asyncio.Queue = asyncio.Queue()
        turn_id = uuid.uuid4().hex
        assert await self.repo.acquire_turn_lock(self.session.id, turn_id)
        result = await O.run_turn(
            session_id=self.session.id, actor=O.TurnActor(USER, ORG), repo=self.repo, facades=self.facades, queue=q,
            text=text, turn_id=turn_id, usage_recorder=_noop,
        )
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        reply = "".join(e[1].text for e in events if e and e[0] == "text_delta")
        return reply, result, [e for e in events if e]

    async def spec(self):
        from services.chat.spec import load_spec

        return load_spec(self.kind, (await self.repo.get_session(self.session.id)).spec)

    async def cards(self):
        return await self.repo.list_cards(self.session.id)

    async def click(self, action: str, payload: dict | None = None, card_id: str | None = None):
        spec = await self.spec()
        cards = await self.cards()
        req = c.ActionRequest(card_id=card_id or str(uuid.uuid4()), action=action, payload=payload or {}, spec_version=spec.version)
        outcome = await K.apply_action(spec, req, self.facades, session_id=self.session.id, existing_cards=cards)
        await self.repo.save_spec(self.session.id, outcome.spec.to_json(), outcome.spec.version)
        for card in outcome.cards:
            await self.repo.upsert_card(self.session.id, org_id=ORG, card=card)
        return outcome


async def _noop(**kw):
    return None


@pytest.fixture
def mock_ai(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")

    def no_real_client():  # nothing may reach the real SDK in mock mode
        raise AssertionError("the real client was constructed under MOCK_AI")

    monkeypatch.setattr("services.llm.get_async_client", no_real_client)


async def test_orchestrator_picks_the_mock_client_when_mock_ai_is_set(mock_ai):
    walk = await Walk("protocol").start()
    reply, result, events = await walk.say("hello")
    assert result.error is None and "drug" in reply.lower()


async def test_protocol_flow_end_to_end_with_zero_credits(mock_ai):
    walk = await Walk("protocol").start()
    reply, result, events = await walk.say("Biosimilar PK study for adalimumab in healthy volunteers, age 18-45")
    assert result.error is None and "Pick the right one on the card" in reply
    spec = await walk.spec()
    assert spec.study_type == "biosimilar_pk" and spec.status_of("study_type") == FieldStatus.confirmed
    assert (spec.population.age_min, spec.population.age_max) == (18, 45)
    assert spec.status_of("population.age_min") == FieldStatus.confirmed  # the quote is verbatim
    assert spec.drug is None  # the drug is chosen by a click, not by the model
    cards = await walk.cards()
    matches = next(x for x in cards if x.type == CardType.drug_matches)
    assert c.DrugMatchesCardData.model_validate(matches.data).matches[0].id == ADA

    # the user clicks "Use adalimumab", then walks the cards
    out = await walk.click("select_drug", {"drug_id": ADA}, card_id=matches.id)
    assert out.spec.drug.inn == "adalimumab" and out.guide.text
    out = await walk.click("update_population", {"patch": {"subject_type": "healthy"}})
    design = next(x for x in await walk.cards() if x.type == CardType.design_decisions and x.status == CardStatus.active)
    out = await walk.click("confirm_design", card_id=design.id)
    out = await walk.click("set_guideline", {"guideline_version": "EMA-BE-2010"})
    assert out.spec.ready()

    reply, result, events = await walk.say("ok, generate it")
    assert "Generate protocol" in reply
    summary = [x for x in await walk.cards() if x.type == CardType.protocol_summary and x.status == CardStatus.active]
    assert len(summary) == 1 and [a.id for a in summary[0].actions] == ["generate_protocol"]

    out = await walk.click("generate_protocol", card_id=summary[0].id)
    assert isinstance(out.intent, K.GenerateProtocolIntent)
    from schemas import ProtocolCreate

    body = ProtocolCreate.model_validate(out.intent.payload)
    assert body.guideline_version == "EMA-BE-2010" and body.population_config["age_max"] == 45
    assert out.intent.provenance["field_provenance"]["population.age_max"]["quote"].lower() in "age 18-45"
    # the mock never created anything itself: no protocol, only an intent for the router
    rows = await walk.repo.list_messages(walk.session.id)
    assert not any("generat" in json.dumps(r.api_content).lower() and r.role == MessageRole.tool_results and "started" in json.dumps(r.api_content).lower() for r in rows)


async def test_protocol_flow_gate_message_reaches_the_user(mock_ai):
    walk = await Walk("protocol").start()
    await walk.say("adalimumab")
    spec = await walk.spec()
    await walk.click("select_drug", {"drug_id": ADA})
    reply, result, events = await walk.say("make it a BE study")
    from routers.protocols import study_type_gate_detail

    assert study_type_gate_detail("adalimumab", "BE") in reply
    study_card = [x for x in await walk.cards() if x.type == CardType.study_type][-1]
    assert [o["value"] for o in study_card.data["options"] if o["allowed"]] == ["biosimilar_pk"]


async def test_protocol_flow_screening_and_design_via_chat(mock_ai):
    walk = await Walk("protocol").start()
    await walk.click("select_drug", {"drug_id": IBU})
    await walk.click("set_study_type", {"study_type": "BE"})
    reply, _, _ = await walk.say("show me the interactions")
    assert "interaction screening is on the card" in reply
    reply, _, _ = await walk.say("review the design")
    assert "Confirm design" in reply
    types = [x.type for x in await walk.cards()]
    assert CardType.interaction_screening in types and CardType.design_decisions in types


async def test_budget_flow_end_to_end(mock_ai):
    walk = await Walk("budget").start()
    reply, result, events = await walk.say("Price a patient study for nivolumab")
    assert result.error is None and "estimate is on the card" in reply
    assert not re.search(r"\d{3}", reply)  # the model never states a price
    cards = await walk.cards()
    assert any(x.type == CardType.budget_preview for x in cards)
    reply, _, _ = await walk.say("use 40 sites")
    spec = await walk.spec()
    assert spec.n_sites_selected == 40 and spec.status_of("n_sites_selected") == FieldStatus.confirmed
    assert spec.drug_inn == "nivolumab" and spec.resolved_subject_profile() == "patient"
    reply, _, _ = await walk.say("show the summary")
    summary = [x for x in await walk.cards() if x.type == CardType.budget_summary][-1]
    assert summary.data["ready"] is True and [a.id for a in summary.actions] == ["create_budget"]
    out = await walk.click("create_budget", card_id=summary.id)
    assert isinstance(out.intent, K.CreateBudgetIntent)
    assert out.intent.payload["n_sites_selected"] == 40 and out.intent.payload["drug_inn"] == "nivolumab"


async def test_edit_flow_prose_and_engine_edits(mock_ai):
    snap = {
        "id": PROTO_ID, "drug_inn": "ibuprofen", "status": "draft", "version": 3, "study_type": "BE", "guideline_version": "FDA-BE-2014",
        "generation_status": "complete", "sections": {"synopsis": "<p>x</p>", "safety_monitoring": "<p>adverse events</p>"},
        "section_labels": {"synopsis": "Synopsis", "safety_monitoring": "Safety Monitoring"}, "inputs": {"age_min": 18}, "decisions": [],
        "design_key": "crossover", "t_half_hours": 48.0, "facts": {"n_enrolled": 28},
    }
    walk = await Walk("protocol_edit", facades=StaticFacades(protocols={PROTO_ID: snap}), protocol_id=PROTO_ID).start()
    reply, result, _ = await walk.say("Please shorten the adverse events section")
    assert result.error is None
    kinds = [x.type for x in await walk.cards()]
    assert CardType.protocol_overview in kinds and CardType.edit_impact in kinds
    impact = [x for x in await walk.cards() if x.type == CardType.edit_impact and x.status == CardStatus.active][-1]
    assert impact.data["sections"] == ["safety_monitoring"] and "Nothing happens until you click" in reply
    out = await walk.click("confirm_edit", {"proposal_id": impact.data["proposal_id"]}, card_id=impact.id)
    assert isinstance(out.intent, K.ConfirmEditIntent) and out.intent.kind == "prose"

    reply, _, _ = await walk.say("make the washout 14 days")
    impact2 = [x for x in await walk.cards() if x.type == CardType.edit_impact and x.status == CardStatus.active][-1]
    assert impact2.data["kind"] == "engine" and impact2.data["changes"][0]["field"] == "washout_days"
    assert impact2.data["changes"][0]["after"] == "14"


async def test_edit_flow_refuses_washout_on_a_parallel_design_and_blocks_signed_protocols(mock_ai):
    base = {
        "id": PROTO_ID, "drug_inn": "adalimumab", "status": "draft", "version": 2, "study_type": "biosimilar_pk",
        "guideline_version": "FDA-BE-2014", "generation_status": "complete", "sections": {"synopsis": "<p>x</p>"},
        "inputs": {}, "decisions": [], "design_key": "parallel", "t_half_hours": 336.0, "facts": {},
    }
    walk = await Walk("protocol_edit", facades=StaticFacades(protocols={PROTO_ID: base}), protocol_id=PROTO_ID).start()
    await walk.say("overview")
    reply, _, _ = await walk.say("make the washout 14 days")
    assert "parallel" in reply and "no washout applies" in reply
    assert not [x for x in await walk.cards() if x.type == CardType.edit_impact]

    signed = Walk("protocol_edit", facades=StaticFacades(protocols={PROTO_ID: {**base, "status": "approved"}}), protocol_id=PROTO_ID)
    await signed.start()
    reply, _, _ = await signed.say("Please shorten the introduction")
    assert "approved" in reply and "draft" in reply
    assert not [x for x in await signed.cards() if x.type == CardType.edit_impact]


async def test_a_whole_mock_conversation_never_flags_numbers_and_replays_cleanly(mock_ai):
    walk = await Walk("protocol").start()
    for text in ["Biosimilar PK study for adalimumab, age 18-45", "why is the washout that long?", "which guidelines can I pick", "generate"]:
        reply, result, events = await walk.say(text)
        assert result.error is None and names_ok(events)
        assert result.flags.unverified_numbers == [], text
    rows = await walk.repo.list_messages(walk.session.id)
    msgs = S.history_to_api_messages(rows)
    assert msgs[0]["role"] == "user" and all(m["role"] in ("user", "assistant", "system") for m in msgs)
    # tool_use / tool_result pairing holds across the whole stored history
    open_ids: set[str] = set()
    for m in msgs:
        if m["role"] == "system" or isinstance(m["content"], str):
            continue
        for b in m["content"]:
            if b["type"] == "tool_use":
                open_ids.add(b["id"])
            elif b["type"] == "tool_result":
                open_ids.discard(b["tool_use_id"])
    assert not open_ids


def names_ok(events) -> bool:
    return events[0][0] == "turn_start" and events[-1][0] == "turn_end"
