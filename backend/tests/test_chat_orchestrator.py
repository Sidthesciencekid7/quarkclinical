"""Pure tests for services/chat/orchestrator.py and store.py: the turn loop against
a scripted fake model (real SDK block types), the in-memory repository, and the
history builder. No database, no network, no credits."""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import Message, TextBlock, ThinkingBlock, ToolUseBlock, Usage

from services.chat import contract as c
from services.chat import orchestrator as O
from services.chat import store as S
from services.chat.contract import CardStatus, CardType, ChatErrorCode, MessageRole, SessionKind
from services.chat.facades import StaticFacades
from services.chat.spec import new_spec

ADA = "00000000-0000-4000-8000-000000000001"
USER = "11111111-1111-4111-8111-111111111111"
ORG = "org-1"

ENV_VARS = (
    "MOCK_AI", "CHAT_MODEL", "LLM_PROVIDER", "CHAT_EFFORT", "CHAT_MAX_TOOL_ITERATIONS", "CHAT_MAX_TOKENS",
    "AWS_REGION", "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# A scripted stand-in for the Anthropic client (real SDK block types)
# ---------------------------------------------------------------------------


def thinking(sig: str = "sig-1") -> ThinkingBlock:
    return ThinkingBlock(type="thinking", thinking="", signature=sig)


def text(s: str) -> TextBlock:
    return TextBlock(type="text", text=s)


def tool(name: str, args: dict[str, Any], block_id: str | None = None) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=block_id or f"toolu_{uuid.uuid4().hex[:8]}", name=name, input=args)


def reply(*blocks, stop: str = "end_turn", usage: dict[str, int] | None = None, details: Any = None, model: str = "claude-opus-5-5") -> Message:
    u = usage or {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 400}
    return Message.model_construct(
        id=f"msg_{uuid.uuid4().hex[:8]}", type="message", role="assistant", model=model, content=list(blocks),
        stop_reason=stop, stop_sequence=None, stop_details=details, usage=Usage(**u),
    )


class _Stream:
    def __init__(self, message: Message | BaseException):
        self.message = message

    async def __aenter__(self):
        if isinstance(self.message, BaseException):
            raise self.message
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for i, block in enumerate(self.message.content):
            if block.type == "text":
                for j in range(0, len(block.text), 7):
                    yield SimpleNamespace(
                        type="content_block_delta", index=i,
                        delta=SimpleNamespace(type="text_delta", text=block.text[j:j + 7]),
                    )
        yield SimpleNamespace(type="message_stop")

    async def get_final_message(self):
        return self.message


class _API:
    def __init__(self, client: "ScriptedClient", namespace: str):
        self.client, self.namespace = client, namespace

    def stream(self, **params):
        self.client.namespaces.append(self.namespace)
        self.client.calls.append(copy.deepcopy(params))
        if not self.client.script:
            raise AssertionError("the scripted client ran out of replies")
        item = self.client.script.pop(0)
        if callable(item) and not isinstance(item, BaseException):
            item = item(params)
        return _Stream(item)


class ScriptedClient:
    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.namespaces: list[str] = []
        self.messages = _API(self, "messages")
        self.beta = SimpleNamespace(messages=_API(self, "beta"))


class Recorder:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kw):
        self.calls.append(kw)


class StatusError(Exception):
    def __init__(self, status_code: int, message: str = "provider said no"):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def new_session(repo: S.InMemoryRepository, kind: str = "protocol", **extra) -> S.SessionRecord:
    spec = new_spec(kind, **extra).to_json()
    return await repo.create_session(org_id=ORG, created_by=USER, kind=SessionKind(kind), spec=spec, spec_version=0)


def drain(q: asyncio.Queue) -> list[Any]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def run_one(
    repo, sid, client, *, text: str | None = "hello", facades=None, recorder=None, queue=None, resume=False,
    client_turn_id="ct-1", turn_id=None,
):
    q = queue if queue is not None else asyncio.Queue()
    turn_id = turn_id or uuid.uuid4().hex
    assert await repo.acquire_turn_lock(sid, turn_id)
    result = await O.run_turn(
        session_id=sid, actor=O.TurnActor(USER, ORG), repo=repo, facades=facades or StaticFacades(), queue=q,
        text=None if resume else text, resume=resume, client_turn_id=client_turn_id, turn_id=turn_id, client=client,
        usage_recorder=recorder or Recorder(),
    )
    return drain(q), result


def names(events) -> list[str | None]:
    return [None if e is None else e[0] for e in events]


def payloads(events, name):
    return [e[1] for e in events if e and e[0] == name]


def assistant_text(events) -> str:
    return "".join(p.text for p in payloads(events, "text_delta"))


# ---------------------------------------------------------------------------
# The tool loop
# ---------------------------------------------------------------------------


async def test_tool_loop_runs_tools_and_replays_the_model_message_verbatim():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    first = reply(thinking("sig-xyz"), text("Let me look."), tool("search_drugs", {"query": "adalimumab"}, "toolu_A"), stop="tool_use")
    client = ScriptedClient(first, reply(text("Pick the right one on the card.")))
    events, result = await run_one(repo, s.id, client, text="a study of adalimumab")

    assert len(client.calls) == 2 and result.model_calls == 2 and result.stop_reason == "end_turn"
    assert names(events)[0] == "turn_start" and names(events)[-1] is None and "turn_end" in names(events)
    assert assistant_text(events) == "Let me look.Pick the right one on the card."
    tool_start = payloads(events, "tool_start")[0]
    assert tool_start.tool == "search_drugs" and "catalogue" in tool_start.label
    card = payloads(events, "card")[0]
    assert card.type == CardType.drug_matches

    # The second call replays the first assistant message exactly: the thinking block
    # with its signature, the text, and the tool_use.
    second_messages = client.calls[1]["messages"]
    assistant = [m for m in second_messages if m["role"] == "assistant"][0]
    assert assistant["content"] == [
        {"type": "thinking", "thinking": "", "signature": "sig-xyz"},
        {"type": "text", "text": "Let me look."},
        {"type": "tool_use", "id": "toolu_A", "name": "search_drugs", "input": {"query": "adalimumab"}},
    ]
    results = second_messages[-1]
    assert results["role"] == "user" and results["content"][0]["type"] == "tool_result"
    assert results["content"][0]["tool_use_id"] == "toolu_A"
    assert json.loads(results["content"][0]["content"])["matches"][0]["inn"] == "adalimumab"


async def test_all_tool_results_of_one_assistant_message_go_back_in_one_user_message():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    parallel = reply(
        tool("search_drugs", {"query": "metformin"}, "toolu_1"), tool("list_guidelines", {}, "toolu_2"),
        tool("explain_rule", {"topic": "washout"}, "toolu_3"), stop="tool_use",
    )
    client = ScriptedClient(parallel, reply(text("Done.")))
    await run_one(repo, s.id, client)
    last = client.calls[1]["messages"][-1]
    assert last["role"] == "user" and [b["tool_use_id"] for b in last["content"]] == ["toolu_1", "toolu_2", "toolu_3"]
    rows = await repo.list_messages(s.id)
    assert [r.role for r in rows].count(MessageRole.tool_results) == 1
    user_rows_to_api = [m for m in client.calls[1]["messages"] if m["role"] == "user"]
    assert all(b["type"] == "tool_result" for b in user_rows_to_api[-1]["content"])


async def test_a_failing_tool_becomes_an_error_result_and_the_turn_continues():
    class Boom(StaticFacades):
        async def search_drugs(self, query, limit=8):
            raise RuntimeError("secret internal detail")

    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(tool("search_drugs", {"query": "metformin"}, "toolu_1"), stop="tool_use"), reply(text("Sorry about that.")))
    events, result = await run_one(repo, s.id, client, facades=Boom())
    block = client.calls[1]["messages"][-1]["content"][0]
    assert block["is_error"] is True and "secret internal detail" not in block["content"]
    assert result.stop_reason == "end_turn" and result.error is None


async def test_unknown_tool_names_are_answered_with_an_error_result():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(tool("delete_everything", {}, "toolu_1"), stop="tool_use"), reply(text("ok")))
    await run_one(repo, s.id, client)
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


# ---------------------------------------------------------------------------
# What every request looks like
# ---------------------------------------------------------------------------


async def test_request_parameters_caching_tools_and_effort():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(text("hi")))
    await run_one(repo, s.id, client)
    p = client.calls[0]
    assert p["model"] == "claude-opus-5-5" and p["max_tokens"] == 4096
    assert p["tool_choice"] == {"type": "auto"}
    assert p["thinking"] == {"type": "adaptive"} and "display" not in p["thinking"]
    assert p["output_config"] == {"effort": "low"}
    # caching: an explicit breakpoint on the system block AND automatic top-level caching
    assert p["cache_control"] == {"type": "ephemeral"}
    assert len(p["system"]) == 1 and p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert p["system"][0]["text"].startswith("You are the study-setup assistant")
    names_ = [t["name"] for t in p["tools"]]
    assert names_ == sorted(names_) and all(t["strict"] is True for t in p["tools"])
    # the system prompt is a constant: nothing dynamic is interpolated into it
    s2 = await new_session(repo)
    client2 = ScriptedClient(reply(text("hi")))
    await run_one(repo, s2.id, client2)
    assert client2.calls[0]["system"] == p["system"] and client2.calls[0]["tools"] == p["tools"]


async def test_server_side_fallbacks_use_the_beta_namespace_on_the_claude_api_only(monkeypatch):
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(text("hi")))
    await run_one(repo, s.id, client)
    assert client.namespaces == ["beta"]
    assert client.calls[0]["betas"] == ["server-side-fallback-2026-07-01"] and client.calls[0]["fallbacks"] == "default"

    monkeypatch.setenv("LLM_PROVIDER", "bedrock")  # not available on Bedrock
    s2 = await new_session(repo)
    client2 = ScriptedClient(reply(text("hi")))
    await run_one(repo, s2.id, client2)
    assert client2.namespaces == ["messages"]
    assert "fallbacks" not in client2.calls[0] and "betas" not in client2.calls[0]
    assert client2.calls[0]["model"] == "anthropic.claude-opus-5-5"


async def test_effort_iterations_and_max_tokens_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("CHAT_EFFORT", "medium")
    monkeypatch.setenv("CHAT_MAX_TOKENS", "2048")
    monkeypatch.setenv("CHAT_MODEL", "claude-sonnet-5-5")
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(text("hi"), model="claude-sonnet-5-5"))
    await run_one(repo, s.id, client)
    p = client.calls[0]
    assert p["output_config"] == {"effort": "medium"} and p["max_tokens"] == 2048 and p["model"] == "claude-sonnet-5-5"
    monkeypatch.setenv("CHAT_EFFORT", "nonsense")
    assert O.chat_effort() == "low"


async def test_the_draft_spec_system_message_follows_the_users_message():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(text("hi")))
    await run_one(repo, s.id, client, text="A biosimilar study please")
    msgs = client.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "system"]
    assert msgs[0]["content"] == [{"type": "text", "text": "A biosimilar study please"}]
    assert msgs[1]["content"].startswith('<draft_spec schema="protocol_draft@1" kind="protocol" version="0">')
    body = json.loads(msgs[1]["content"].split("\n")[1])
    assert body["missing"] == ["drug", "study_type", "design_decisions"] and body["next_step"] == "drug"


async def test_models_without_mid_conversation_system_get_a_trailing_reminder_block(monkeypatch):
    monkeypatch.setenv("CHAT_MODEL", "claude-sonnet-4-6")
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(text("hi"), model="claude-sonnet-4-6"))
    await run_one(repo, s.id, client, text="hello there")
    msgs = client.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["user"]
    assert msgs[0]["content"][0] == {"type": "text", "text": "hello there"}
    assert msgs[0]["content"][1]["text"].startswith("<system-reminder>\n<draft_spec")
    assert client.namespaces == ["messages"]  # no server-side fallbacks for this model


async def test_card_clicks_since_the_last_turn_are_listed_in_the_spec_message():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    await repo.append_message(s.id, org_id=ORG, role=MessageRole.ui_action, api_content=None, display={"text": "Selected adalimumab"}, created_by=USER)
    client = ScriptedClient(reply(text("ok")))
    await run_one(repo, s.id, client)
    system = client.calls[0]["messages"][-1]["content"]
    assert "<ui_actions_since_last_turn>\n- Selected adalimumab\n</ui_actions_since_last_turn>" in system
    client2 = ScriptedClient(reply(text("ok")))
    await run_one(repo, s.id, client2, text="next")
    assert "ui_actions_since_last_turn" not in client2.calls[0]["messages"][-1]["content"]


# ---------------------------------------------------------------------------
# History is append-only
# ---------------------------------------------------------------------------


async def test_history_only_grows_between_calls_and_turns():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(
        reply(thinking("a"), tool("search_drugs", {"query": "metformin"}, "toolu_1"), stop="tool_use"),
        reply(thinking("b"), text("First answer.")),
        reply(thinking("c"), tool("list_guidelines", {}, "toolu_2"), stop="tool_use"),
        reply(text("Second answer.")),
    )
    await run_one(repo, s.id, client, text="first question")
    await run_one(repo, s.id, client, text="second question")
    assert len(client.calls) == 4
    for earlier, later in zip(client.calls, client.calls[1:]):
        prefix = later["messages"][: len(earlier["messages"])]
        assert prefix == earlier["messages"], "an earlier request is not a prefix of the next one"
    # ... and the very first call's draft_spec message is still byte-identical in the last call.
    assert client.calls[3]["messages"][1] == client.calls[0]["messages"][1]
    rows = await repo.list_messages(s.id)
    assert [r.seq for r in rows] == list(range(1, len(rows) + 1))


async def test_a_turn_that_failed_leaves_no_dangling_system_message_for_the_next_call():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    failing = ScriptedClient(StatusError(529))
    events, result = await run_one(repo, s.id, failing, text="first")
    assert result.error and result.error.retryable and result.error.code == ChatErrorCode.model_error.value
    assert "turn_end" not in names(events) and names(events)[-1] is None
    ok = ScriptedClient(reply(text("fine")))
    await run_one(repo, s.id, ok, text="second")
    roles = [m["role"] for m in ok.calls[0]["messages"]]
    assert roles == ["user", "user", "system"]  # the failed turn's note is dropped; two user turns merge server-side


# ---------------------------------------------------------------------------
# Usage ledger
# ---------------------------------------------------------------------------


async def test_a_usage_record_is_written_for_every_model_call():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    rec = Recorder()
    client = ScriptedClient(
        reply(tool("list_guidelines", {}), stop="tool_use", usage={"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 0}),
        reply(text("done"), usage={"input_tokens": 12, "output_tokens": 7, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 110}),
    )
    await run_one(repo, s.id, client, recorder=rec)
    assert len(rec.calls) == 2
    first, second = rec.calls
    assert first["feature"] == "chat" and first["org_id"] == ORG and first["user_id"] == USER
    assert first["resource_type"] == "chat_session" and first["resource_id"] == s.id and first["model"] == "claude-opus-5-5"
    assert first["usage"].cache_creation_input_tokens == 100 and second["usage"].cache_read_input_tokens == 110
    rows = await repo.list_messages(s.id)
    assistant_usage = [r.usage for r in rows if r.role == MessageRole.assistant]
    assert assistant_usage[1] == {"input_tokens": 12, "output_tokens": 7, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 110}


async def test_a_ledger_failure_never_fails_the_turn():
    async def broken(**kw):
        raise RuntimeError("ledger down")

    repo = S.InMemoryRepository()
    s = await new_session(repo)
    events, result = await run_one(repo, s.id, ScriptedClient(reply(text("fine"))), recorder=broken)
    assert result.error is None and result.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# Stop reasons
# ---------------------------------------------------------------------------


async def test_refusal_shows_a_notice_card_and_runs_nothing():
    f = StaticFacades()
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(stop="refusal", details=SimpleNamespace(type="refusal", category="bio", explanation=None)))
    events, result = await run_one(repo, s.id, client, facades=f)
    assert result.stop_reason == "refusal" and len(client.calls) == 1
    card = payloads(events, "card")[0]
    assert card.type == CardType.notice and card.data["level"] == "warning" and card.data["code"] == "bio"
    end = payloads(events, "turn_end")[0]
    assert end.stop_reason == "refusal" and end.message_id == result.message_id
    rows = await repo.list_messages(s.id)
    refused = [r for r in rows if r.role == MessageRole.assistant][0]
    assert refused.api_content is None and refused.stop_reason == "refusal"  # empty content is never replayed
    assert refused.display["card_ids"] == [card.id]
    nxt = ScriptedClient(reply(text("ok")))
    await run_one(repo, s.id, nxt, text="try again")
    assert [m["role"] for m in nxt.calls[0]["messages"]] == ["user", "user", "system"]


async def test_refusal_that_cut_off_a_tool_call_still_pairs_the_tool_use():
    f = StaticFacades()
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(text("Let me"), tool("search_drugs", {"query": "x"}, "toolu_cut"), stop="refusal"))
    await run_one(repo, s.id, client, facades=f)
    assert f.called("search_drugs") == []  # never run
    rows = await repo.list_messages(s.id)
    results = [r for r in rows if r.role == MessageRole.tool_results][0]
    assert results.api_content[0]["tool_use_id"] == "toolu_cut" and results.api_content[0]["is_error"] is True
    nxt = ScriptedClient(reply(text("ok")))
    await run_one(repo, s.id, nxt, text="again")
    msgs = nxt.calls[0]["messages"]
    assert [m["role"] for m in msgs[:4]] == ["user", "system", "assistant", "user"]
    assert msgs[3]["content"][0]["type"] == "tool_result" and msgs[3]["content"][0]["is_error"] is True


async def test_max_tokens_with_a_tool_use_does_not_run_the_tool():
    f = StaticFacades()
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(text("Searching"), tool("search_drugs", {"query": "adali"}, "toolu_1"), stop="max_tokens"))
    events, result = await run_one(repo, s.id, client, facades=f)
    assert f.called("search_drugs") == [] and result.stop_reason == "max_tokens" and len(client.calls) == 1
    notice = payloads(events, "notice")[0]
    assert notice.level == "info" and "cut off" in notice.text
    assert payloads(events, "turn_end")[0].stop_reason == "max_tokens"
    rows = await repo.list_messages(s.id)
    err = [r for r in rows if r.role == MessageRole.tool_results][0].api_content[0]
    assert err["is_error"] is True and err["tool_use_id"] == "toolu_1"


async def test_max_tokens_on_plain_text_just_ends_the_turn_with_a_notice():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    events, result = await run_one(repo, s.id, ScriptedClient(reply(text("A very long reply that"), stop="max_tokens")))
    assert result.stop_reason == "max_tokens" and payloads(events, "notice")


async def test_pause_turn_continues_with_the_paused_message_appended():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(text("Part one. "), stop="pause_turn"), reply(text("Part two.")))
    events, result = await run_one(repo, s.id, client)
    assert len(client.calls) == 2 and result.stop_reason == "end_turn"
    assert client.calls[1]["messages"][-1] == {"role": "assistant", "content": [{"type": "text", "text": "Part one. "}]}


async def test_iteration_limit_stops_a_runaway_tool_loop(monkeypatch):
    monkeypatch.setenv("CHAT_MAX_TOOL_ITERATIONS", "2")
    f = StaticFacades()
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    loop = lambda p: reply(tool("search_drugs", {"query": "metformin"}), stop="tool_use")  # noqa: E731
    client = ScriptedClient(loop, loop, loop)
    events, result = await run_one(repo, s.id, client, facades=f)
    assert len(client.calls) == 2 and len(f.called("search_drugs")) == 2
    assert result.stop_reason == "iteration_limit" and payloads(events, "turn_end")[0].stop_reason == "iteration_limit"
    assert "step limit" in payloads(events, "notice")[0].text
    assert O.max_iterations() == 2


async def test_default_iteration_limit_is_the_contract_value():
    assert O.max_iterations() == c.CHAT_MAX_TOOL_ITERATIONS == 8


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,retryable", [(429, True), (500, True), (529, True), (400, False), (401, False)])
async def test_model_errors_become_user_safe_error_events(status, retryable):
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    events, result = await run_one(repo, s.id, ScriptedClient(StatusError(status, "RAW PROVIDER TEXT sk-ant-secret")))
    err = payloads(events, "error")[0]
    assert err.retryable is retryable and "RAW PROVIDER TEXT" not in err.message and "sk-ant" not in err.message
    assert result.error == err and names(events)[-1] is None
    assert not (await repo.get_session(s.id)).turn_lock_id  # lock released


def test_classify_error_for_timeouts_and_unknown_errors():
    assert O.classify_error(asyncio.TimeoutError()).retryable
    assert O.classify_error(ValueError("x")).code == ChatErrorCode.internal.value


async def test_an_unexpected_exception_is_contained_and_the_lock_is_released():
    class Exploding(S.InMemoryRepository):
        async def append_message(self, *a, **k):
            if k.get("role") == MessageRole.assistant:
                raise RuntimeError("disk full")
            return await super().append_message(*a, **k)

    repo = Exploding()
    s = await new_session(repo)
    events, result = await run_one(repo, s.id, ScriptedClient(reply(text("hi"))))
    assert payloads(events, "error")[0].code == ChatErrorCode.internal.value
    assert names(events)[-1] is None and not (await repo.get_session(s.id)).turn_lock_id


async def test_empty_and_resume_turn_validation():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    events, _ = await run_one(repo, s.id, ScriptedClient(), text="  \x00 ")
    assert payloads(events, "error")[0].code == ChatErrorCode.validation.value
    events, _ = await run_one(repo, s.id, ScriptedClient(), resume=True)
    assert payloads(events, "error")[0].code == ChatErrorCode.invalid_action.value
    q: asyncio.Queue = asyncio.Queue()
    result = await O.run_turn(
        session_id=str(uuid.uuid4()), actor=O.TurnActor(USER, ORG), repo=repo, facades=StaticFacades(), queue=q,
        text="hi", client=ScriptedClient(), usage_recorder=Recorder(),
    )
    assert result.error.code == ChatErrorCode.not_found.value


async def test_resume_answers_the_stored_initial_message():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    await repo.append_message(
        s.id, org_id=ORG, role=MessageRole.user, api_content=[{"type": "text", "text": "Biosimilar study of adalimumab"}],
        display={"text": "Biosimilar study of adalimumab", "card_ids": [], "flags": {}}, created_by=USER,
    )
    assert S.awaiting_reply(await repo.list_messages(s.id))
    client = ScriptedClient(reply(text("Welcome.")))
    events, result = await run_one(repo, s.id, client, resume=True)
    assert result.error is None and assistant_text(events) == "Welcome."
    msgs = client.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "system"] and msgs[0]["content"][0]["text"] == "Biosimilar study of adalimumab"
    assert not S.awaiting_reply(await repo.list_messages(s.id))


# ---------------------------------------------------------------------------
# Persistence, cards, spec
# ---------------------------------------------------------------------------


async def test_cards_spec_and_display_are_persisted_as_the_turn_goes():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(
        reply(text("Searching."), tool("search_drugs", {"query": "adalimumab"}, "toolu_1"),
              tool("update_draft", {"changes": [{"field": "study_type", "value": "biosimilar_pk", "user_quote": "biosimilar"}]}, "toolu_2"), stop="tool_use"),
        reply(text("All set.")),
    )
    events, result = await run_one(repo, s.id, client, text="a biosimilar study of adalimumab")
    spec_events = payloads(events, "spec")
    assert len(spec_events) == 1 and spec_events[0].summary.fields
    saved = await repo.get_session(s.id)
    assert saved.spec["study_type"] == "biosimilar_pk" and saved.spec_version == spec_events[0].spec_version > 0
    assert repo.spec_saves and repo.spec_saves[-1][1] == saved.spec_version
    cards = await repo.list_cards(s.id)
    assert {x.type for x in cards} == {CardType.drug_matches}
    rows = await repo.list_messages(s.id)
    tool_msg = [r for r in rows if r.role == MessageRole.assistant][0]
    assert tool_msg.display["card_ids"] == [cards[0].id] and tool_msg.display["text"] == "Searching."
    assert tool_msg.api_content[0]["type"] == "text"
    user = rows[0]
    assert user.display["client_turn_id"] == "ct-1" and user.created_by == USER
    assert (await repo.find_user_message_by_client_turn(s.id, "ct-1")).id == user.id
    assert (await repo.get_session(s.id)).title == "a biosimilar study of adalimumab"


async def test_a_newer_card_of_the_same_type_supersedes_the_older_one_and_says_so():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(
        reply(tool("search_drugs", {"query": "adalimumab"}), stop="tool_use"), reply(text("one")),
        reply(tool("search_drugs", {"query": "metformin"}), stop="tool_use"), reply(text("two")),
    )
    await run_one(repo, s.id, client, text="first")
    events, _ = await run_one(repo, s.id, client, text="second")
    cards = await repo.list_cards(s.id)
    assert [x.status for x in cards] == [CardStatus.superseded, CardStatus.active]
    card_events = payloads(events, "card")
    assert [x.status for x in card_events] == [CardStatus.superseded, CardStatus.active]  # the client is told, in order


async def test_open_cards_follow_the_spec_after_a_tool_changes_it():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(
        reply(tool("update_draft", {"changes": [{"field": "age_max", "value": "45", "user_quote": "45"}]}), stop="tool_use"),
        reply(text("a")),
        reply(tool("update_draft", {"changes": [{"field": "sex", "value": "male", "user_quote": "men"}]}), stop="tool_use"),
        reply(text("b")),
    )
    await run_one(repo, s.id, client, text="up to 45")
    await run_one(repo, s.id, client, text="men only")
    pop = [x for x in await repo.list_cards(s.id) if x.type == CardType.population]
    assert [x.status for x in pop] == [CardStatus.superseded, CardStatus.active]
    assert pop[-1].data["values"]["sex"] == "male" and pop[-1].data["values"]["age_max"] == 45


async def test_the_number_check_flags_figures_no_tool_or_user_gave():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(
        reply(tool("explain_rule", {"topic": "washout"}, "toolu_1"), stop="tool_use"),
        reply(text("The floor is 7 x t1/2 and the ceiling is 21 days, so you would need 64 subjects.")),
    )
    events, result = await run_one(repo, s.id, client)
    assert result.flags.unverified_numbers == ["64"]  # 21 is in the explain_rule text; 64 is made up
    assert payloads(events, "turn_end")[0].flags.unverified_numbers == ["64"]
    rows = await repo.list_messages(s.id)
    last = [r for r in rows if r.role == MessageRole.assistant][-1]
    assert last.display["flags"]["unverified_numbers"] == ["64"]


async def test_numbers_the_user_typed_are_not_flagged():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    events, result = await run_one(repo, s.id, ScriptedClient(reply(text("So ages 18 to 45 it is."))), text="age 18-45 please")
    assert result.flags.unverified_numbers == []


# ---------------------------------------------------------------------------
# Disconnects and cancellation
# ---------------------------------------------------------------------------


async def test_the_turn_completes_and_persists_when_nobody_reads_the_stream():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    q: asyncio.Queue = asyncio.Queue(maxsize=1)  # a consumer that is gone: the queue fills up immediately
    client = ScriptedClient(
        reply(text("Working."), tool("search_drugs", {"query": "adalimumab"}, "toolu_1"), stop="tool_use"),
        reply(text("Finished.")),
    )
    assert await repo.acquire_turn_lock(s.id, "t1")
    result = await O.run_turn(
        session_id=s.id, actor=O.TurnActor(USER, ORG), repo=repo, facades=StaticFacades(), queue=q, text="hello",
        turn_id="t1", client=client, usage_recorder=Recorder(),
    )
    assert result.stop_reason == "end_turn" and len(client.calls) == 2
    rows = await repo.list_messages(s.id)
    roles = [r.role for r in rows]
    assert roles == [MessageRole.user, MessageRole.system_note, MessageRole.assistant, MessageRole.tool_results, MessageRole.assistant]
    assert rows[-1].display["text"] == "Finished."
    assert len(await repo.list_cards(s.id)) == 1 and not (await repo.get_session(s.id)).turn_lock_id


async def test_a_consumer_that_stops_reading_mid_stream_does_not_stop_the_turn():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    q: asyncio.Queue = asyncio.Queue()
    client = ScriptedClient(reply(text("A fairly long answer that streams in many small pieces.")))
    assert await repo.acquire_turn_lock(s.id, "t1")
    task = asyncio.create_task(O.run_turn(
        session_id=s.id, actor=O.TurnActor(USER, ORG), repo=repo, facades=StaticFacades(), queue=q, text="hi",
        turn_id="t1", client=client, usage_recorder=Recorder(),
    ))
    first = await q.get()  # read one event, then "disconnect"
    assert first[0] == "turn_start"
    result = await task
    assert result.stop_reason == "end_turn"
    rows = await repo.list_messages(s.id)
    assert rows[-1].role == MessageRole.assistant and rows[-1].display["text"].startswith("A fairly long answer")


async def test_cancelling_a_turn_mid_tool_closes_the_tool_use_pair():
    started = asyncio.Event()

    class Slow(StaticFacades):
        async def search_drugs(self, query, limit=8):
            started.set()
            await asyncio.sleep(30)
            return []

    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(tool("search_drugs", {"query": "adalimumab"}, "toolu_slow"), stop="tool_use"))
    assert await repo.acquire_turn_lock(s.id, "t1")
    task = asyncio.create_task(O.run_turn(
        session_id=s.id, actor=O.TurnActor(USER, ORG), repo=repo, facades=Slow(), queue=asyncio.Queue(), text="hi",
        turn_id="t1", client=client, usage_recorder=Recorder(),
    ))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = await repo.list_messages(s.id)
    results = [r for r in rows if r.role == MessageRole.tool_results]
    assert len(results) == 1 and results[0].api_content[0]["tool_use_id"] == "toolu_slow" and results[0].api_content[0]["is_error"]
    assert not (await repo.get_session(s.id)).turn_lock_id
    # the stored history is valid for the next call
    nxt = ScriptedClient(reply(text("ok")))
    await run_one(repo, s.id, nxt, text="again")
    msgs = nxt.calls[0]["messages"]
    assert msgs[2]["role"] == "assistant" and msgs[3]["content"][0]["type"] == "tool_result"


# ---------------------------------------------------------------------------
# SSE framing helper
# ---------------------------------------------------------------------------


async def test_iter_sse_frames_formats_events_pings_when_idle_and_stops_at_the_sentinel():
    q: asyncio.Queue = asyncio.Queue()
    frames: list[str] = []

    async def consume():
        async for frame in O.iter_sse_frames(q, heartbeat_seconds=0.02):
            frames.append(frame)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.07)
    q.put_nowait(("text_delta", c.TextDeltaEvent(text="Hi")))
    q.put_nowait(None)
    await asyncio.wait_for(task, 2)
    assert frames[0] == c.SSE_PING
    assert frames[-1] == 'event: text_delta\ndata: {"text":"Hi"}\n\n'


async def test_every_event_a_real_turn_emits_is_a_valid_sse_frame():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    client = ScriptedClient(reply(tool("search_drugs", {"query": "adalimumab"}), stop="tool_use"), reply(text("Done.")))
    events, _ = await run_one(repo, s.id, client)
    for e in events:
        if e is not None:
            frame = c.format_sse(*e)
            assert frame.startswith("event: ") and frame.endswith("\n\n") and frame.count("\ndata: ") == 1


# ---------------------------------------------------------------------------
# build_request / echo_blocks / usage_dict
# ---------------------------------------------------------------------------


def test_build_request_shape():
    ns, p = O.build_request(model="m", kind=SessionKind.budget, messages=[], effort="low", max_tokens=10, server_fallbacks=False)
    assert ns == "messages" and "fallbacks" not in p
    assert [t["name"] for t in p["tools"]] == ["explain_rule", "list_my_protocols", "preview_budget", "show_budget_summary", "update_budget_draft"]
    ns, p = O.build_request(model="m", kind=SessionKind.protocol, messages=[], effort="low", max_tokens=10, server_fallbacks=True)
    assert ns == "beta" and p["fallbacks"] == "default" and p["betas"] == ["server-side-fallback-2026-07-01"]


def test_echo_blocks_drops_what_precedes_a_fallback_boundary():
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "old"},
        {"type": "text", "text": "partial"},
        {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "thinking", "thinking": "", "signature": "new"},
        {"type": "text", "text": "final"},
        {"type": "text", "text": "   "},
    ]
    assert [b.get("signature") or b.get("text") for b in O.echo_blocks(blocks)] == ["partial", "new", "final"]


def test_block_to_dict_handles_sdk_blocks_dicts_and_plain_objects():
    assert O.block_to_dict(tool("a", {"x": 1}, "id1")) == {"type": "tool_use", "id": "id1", "name": "a", "input": {"x": 1}}
    assert O.block_to_dict({"type": "text", "text": "t"}) == {"type": "text", "text": "t"}
    assert O.block_to_dict(SimpleNamespace(type="text", text="p")) == {"type": "text", "text": "p"}
    assert O.usage_dict(None) == {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


# ===========================================================================
# store.py
# ===========================================================================


async def test_transcripts_are_visible_only_to_their_creator():
    repo = S.InMemoryRepository()
    mine = await new_session(repo)
    other_user = await repo.create_session(org_id=ORG, created_by="22222222-2222-4222-8222-222222222222", kind=SessionKind.budget)
    foreign_org = await repo.create_session(org_id="org-2", created_by=USER, kind=SessionKind.budget)

    assert (await repo.get_session_for_viewer(mine.id, org_id=ORG, viewer_id=USER)).id == mine.id
    with pytest.raises(S.ChatAccessError) as same_org_other_user:
        await repo.get_session_for_viewer(other_user.id, org_id=ORG, viewer_id=USER)
    assert same_org_other_user.value.code == ChatErrorCode.forbidden
    with pytest.raises(S.ChatAccessError) as other_org:
        await repo.get_session_for_viewer(foreign_org.id, org_id=ORG, viewer_id=USER)
    assert other_org.value.code == ChatErrorCode.forbidden
    with pytest.raises(S.ChatAccessError) as missing:
        await repo.get_session_for_viewer(str(uuid.uuid4()), org_id=ORG, viewer_id=USER)
    assert missing.value.code == ChatErrorCode.not_found

    listed = await repo.list_sessions(org_id=ORG, created_by=USER)
    assert [r.id for r in listed] == [mine.id]
    assert await repo.list_sessions(org_id=ORG, created_by=USER, kind=SessionKind.budget) == []


async def test_messages_are_append_only_and_display_updates_never_touch_api_content():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    a = await repo.append_message(s.id, org_id=ORG, role=MessageRole.assistant, api_content=[{"type": "text", "text": "hi"}], display={"text": "hi"})
    b = await repo.append_message(s.id, org_id=ORG, role=MessageRole.user, api_content=[{"type": "text", "text": "yo"}], display={"text": "yo"})
    assert (a.seq, b.seq) == (1, 2)
    await repo.set_message_display(a.id, {"text": "hi", "card_ids": ["c1"]})
    rows = await repo.list_messages(s.id)
    assert rows[0].api_content == [{"type": "text", "text": "hi"}] and rows[0].display["card_ids"] == ["c1"]
    rows[0].api_content.append("tamper")  # a returned copy cannot alter storage
    assert (await repo.list_messages(s.id))[0].api_content == [{"type": "text", "text": "hi"}]


async def test_turn_lock_is_exclusive_and_goes_stale():
    now = [datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)]
    repo = S.InMemoryRepository(clock=lambda: now[0])
    s = await new_session(repo)
    assert await repo.acquire_turn_lock(s.id, "a")
    assert not await repo.acquire_turn_lock(s.id, "b")
    assert await repo.is_turn_locked(s.id)
    await repo.release_turn_lock(s.id, "not-the-holder")
    assert await repo.is_turn_locked(s.id)
    now[0] += timedelta(seconds=S.turn_lock_stale_seconds() + 1)
    assert not await repo.is_turn_locked(s.id)  # abandoned by a crashed worker
    assert await repo.acquire_turn_lock(s.id, "b")
    await repo.touch_turn_lock(s.id, "b")
    await repo.release_turn_lock(s.id, "b")
    assert await repo.acquire_turn_lock(s.id, "c")


async def test_counts_for_the_turn_caps():
    repo = S.InMemoryRepository()
    s = await new_session(repo)
    for _ in range(3):
        await repo.append_message(s.id, org_id=ORG, role=MessageRole.user, api_content=[{"type": "text", "text": "x"}], display={"text": "x"}, created_by=USER)
    await repo.append_message(s.id, org_id=ORG, role=MessageRole.assistant, api_content=[{"type": "text", "text": "y"}], display={"text": "y"})
    assert await repo.count_user_messages(s.id) == 3
    assert await repo.count_user_messages_since(USER, datetime.now(timezone.utc) - timedelta(days=1)) == 3
    assert await repo.count_user_messages_since(USER, datetime.now(timezone.utc) + timedelta(days=1)) == 0


def row(seq, role, api_content=None, text_=""):
    return S.MessageRecord(id=str(seq), session_id="s", org_id=ORG, seq=seq, role=role, api_content=api_content, display={"text": text_})


def test_history_builder_places_system_messages_only_where_the_api_allows():
    U, A, T_, N = MessageRole.user, MessageRole.assistant, MessageRole.tool_results, MessageRole.system_note
    rows = [
        row(1, U, [{"type": "text", "text": "q1"}]), row(2, N, "snapshot 1"),                      # dropped: next is a user turn
        row(3, U, [{"type": "text", "text": "q2"}]), row(4, N, "snapshot 2"),                      # kept: followed by assistant
        row(5, A, [{"type": "text", "text": "a"}]),
        row(6, MessageRole.ui_action, None), row(7, A, None),                                      # skipped: no content
        row(8, U, [{"type": "text", "text": "q3"}]), row(9, N, "snapshot 3"),                      # kept: last
    ]
    msgs = S.history_to_api_messages(rows)
    assert [(m["role"], m["content"] if isinstance(m["content"], str) else m["content"][0]["text"]) for m in msgs] == [
        ("user", "q1"), ("user", "q2"), ("system", "snapshot 2"), ("assistant", "a"), ("user", "q3"), ("system", "snapshot 3"),
    ]


def test_history_builder_folds_notes_into_the_user_message_when_system_is_unsupported():
    rows = [row(1, MessageRole.user, [{"type": "text", "text": "q"}]), row(2, MessageRole.system_note, "snap")]
    msgs = S.history_to_api_messages(rows, supports_system=False)
    assert msgs == [{"role": "user", "content": [
        {"type": "text", "text": "q"}, {"type": "text", "text": "<system-reminder>\nsnap\n</system-reminder>"}]}]


def test_history_builder_is_deterministic_and_copies():
    rows = [row(1, MessageRole.user, [{"type": "text", "text": "q"}]), row(2, MessageRole.assistant, [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "a"}])]
    a, b = S.history_to_api_messages(rows), S.history_to_api_messages(rows)
    assert a == b and json.dumps(a) == json.dumps(b)
    a[1]["content"].clear()
    assert rows[1].api_content  # the stored row is untouched


def test_message_display_hides_internal_rows():
    assert S.message_display(row(1, MessageRole.tool_results, [{"type": "tool_result"}])) is None
    assert S.message_display(row(2, MessageRole.system_note, "x")) is None
    assert S.message_display(row(3, MessageRole.assistant, None, "")) is None
    shown = S.message_display(row(4, MessageRole.user, [{"type": "text", "text": "q"}], "q"))
    assert shown.text == "q" and shown.role == MessageRole.user


def test_awaiting_reply_ignores_clicks_and_snapshots():
    U, A = MessageRole.user, MessageRole.assistant
    assert S.awaiting_reply([row(1, U, [{}]), row(2, MessageRole.system_note, "s")]) is True
    assert S.awaiting_reply([row(1, U, [{}]), row(2, A, [{}]), row(3, MessageRole.ui_action, None)]) is False
    assert S.awaiting_reply([]) is False


@pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning")
def test_request_parameters_are_accepted_by_the_installed_sdk():
    """Every keyword the orchestrator sends exists on the SDK's stream() signature,
    and the SDK builds a stream manager from them without touching the network."""
    import inspect

    import anthropic
    from anthropic.resources.beta.messages import AsyncMessages as BetaAsyncMessages
    from anthropic.resources.messages import AsyncMessages

    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}, {"role": "system", "content": "<draft_spec/>"}]
    ns, plain = O.build_request(model="claude-opus-5-5", kind=SessionKind.protocol, messages=msgs, effort="low", max_tokens=4096, server_fallbacks=False)
    ns2, beta = O.build_request(model="claude-opus-5-5", kind=SessionKind.protocol, messages=msgs, effort="low", max_tokens=4096, server_fallbacks=True)
    assert (ns, ns2) == ("messages", "beta")
    assert set(plain) <= set(inspect.signature(AsyncMessages.stream).parameters)
    assert set(beta) <= set(inspect.signature(BetaAsyncMessages.stream).parameters)
    client = anthropic.AsyncAnthropic(api_key="sk-test-not-used")
    assert client.messages.stream(**plain) is not None
    assert client.beta.messages.stream(**beta) is not None
