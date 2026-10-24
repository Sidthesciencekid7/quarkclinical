"""The chat turn loop (plan B6).

``run_turn`` runs ONE user turn to completion and pushes contract SSE payloads
into an ``asyncio.Queue`` as it goes; the router (next wave) streams the queue
to the browser. The turn never waits on the consumer, so a browser disconnect
cannot stop it: every model message and every tool result is persisted as soon
as it exists, which keeps each ``tool_use`` paired with its ``tool_result`` and
lets the client recover the transcript with ``GET /api/chat/{sid}``.

Queue protocol: items are ``(event_name, payload_model)`` tuples (format them with
``contract.format_sse``) and a final ``None`` sentinel after the turn ends,
whatever happened. :func:`iter_sse_frames` turns a queue into SSE text with the
``: ping`` heartbeat.

What one call to the model looks like (per the claude-api skill for
claude-opus-5-5 / claude-sonnet-5-5):

* ``system``: ONE fixed text block per session kind with an explicit
  ``cache_control`` breakpoint (it also caches the sorted, strict tool list that
  renders before it), plus a top-level ``cache_control`` so the growing history
  caches automatically. Dynamic state never touches the prefix: the draft snapshot
  is a mid-conversation ``role: "system"`` message AFTER the user's message
  (or, for a model without that feature, a trailing ``<system-reminder>`` block).
* ``tool_choice: {"type": "auto"}`` ONLY (forced choice is a 400 on 5.5 models);
  strict tools; no eager input streaming (inputs are small and strict validation
  matters more).
* ``thinking: {"type": "adaptive"}`` with the default display (omitted), and
  ``output_config: {"effort": CHAT_EFFORT}`` (default ``low``). Thinking blocks
  are persisted and replayed VERBATIM; history is append-only (preserved thinking
  rejects edited history on newer accounts).
* ``max_tokens`` 4096 per call, at most ``CHAT_MAX_TOOL_ITERATIONS`` calls per turn.
* On the Claude API only, ``client.beta.messages.stream`` with
  ``betas=["server-side-fallback-2026-07-01"]`` and ``fallbacks="default"`` so a
  classifier refusal (the 5.5 models add a ``bio`` category that can fire on
  biologics) is retried on Anthropic's recommended model inside the same call.
* Stop reasons: ``end_turn`` finishes; ``tool_use`` runs the tools (ALL results of
  one assistant message go back in ONE user message); ``pause_turn`` continues;
  ``max_tokens`` with a ``tool_use`` present runs nothing; ``refusal`` shows a
  notice card and runs nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from services.chat import cards as K
from services.chat import contract as c
from services.chat import prompts, safety, tools
from services.chat.contract import (
    CHAT_MAX_TOKENS_PER_CALL,
    CHAT_MAX_TOOL_ITERATIONS,
    MAX_TURN_TEXT,
    SSE_HEARTBEAT_SECONDS,
    SSE_PING,
    ChatErrorCode,
    MessageFlags,
    MessageRole,
    SessionKind,
    format_sse,
)
from services.chat.facades import ChatFacades
from services.chat.spec import DraftSpec, EditSessionSpec, load_spec
from services.chat.store import (
    ChatRepository,
    MessageRecord,
    SessionRecord,
    history_to_api_messages,
)

logger = logging.getLogger(__name__)


def _log_failure(what: str, exc: BaseException) -> None:
    """Log a failure WITHOUT the traceback at error level. Transcripts must not
    reach Sentry (B7): its logging integration attaches stack-frame locals (which
    hold user text and tool results) to exception events, so the class name and
    HTTP status are logged and the full traceback only at DEBUG."""
    logger.error("%s: %s status=%s", what, type(exc).__name__, _get(exc, "status_code"))
    logger.debug("%s (traceback)", what, exc_info=True)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
TOOL_TIMEOUT_SECONDS = 120.0
_MONEY_KEYS = ("estimated_grand_total", "grand_total")
_RECENT_CARDS_FOR_EVIDENCE = 12

#: Queue item type: (SSE event name, payload model) or None (end of turn).
SseItem = "tuple[str, Any] | None"

UsageRecorder = Callable[..., Awaitable[None]]


# ---------------------------------------------------------------------------
# Configuration (read at call time)
# ---------------------------------------------------------------------------


def chat_effort() -> str:
    value = (os.getenv("CHAT_EFFORT") or "low").strip().lower()
    return value if value in _EFFORTS else "low"


def max_iterations() -> int:
    try:
        return max(1, int(os.getenv("CHAT_MAX_TOOL_ITERATIONS", CHAT_MAX_TOOL_ITERATIONS)))
    except ValueError:
        return CHAT_MAX_TOOL_ITERATIONS


def max_tokens_per_call() -> int:
    try:
        return max(256, int(os.getenv("CHAT_MAX_TOKENS", CHAT_MAX_TOKENS_PER_CALL)))
    except ValueError:
        return CHAT_MAX_TOKENS_PER_CALL


def mock_enabled() -> bool:
    return bool((os.getenv("MOCK_AI") or "").strip())


def build_request(
    *, model: str, kind: SessionKind, messages: list[dict[str, Any]], effort: str, max_tokens: int,
    server_fallbacks: bool,
) -> tuple[str, dict[str, Any]]:
    """``(namespace, params)`` for one model call. ``namespace`` is ``"messages"``
    or ``"beta"`` (only when server-side fallbacks are requested)."""
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": prompts.system_blocks(kind),
        "tools": tools.tool_definitions(kind),
        "tool_choice": {"type": "auto"},
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "messages": messages,
        "cache_control": {"type": "ephemeral"},
    }
    if server_fallbacks:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
        return "beta", params
    return "messages", params


# ---------------------------------------------------------------------------
# Result / helpers
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    turn_id: str
    message_id: str | None = None
    stop_reason: str = "error"
    flags: MessageFlags = field(default_factory=MessageFlags)
    error: c.ErrorEvent | None = None
    model_calls: int = 0


async def iter_sse_frames(
    queue: "asyncio.Queue[Any]", heartbeat_seconds: float = SSE_HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Drain a turn's queue as SSE frames, emitting ``: ping`` while idle, until the
    end-of-turn sentinel."""
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
        except asyncio.TimeoutError:
            yield SSE_PING
            continue
        if item is None:
            return
        name, payload = item
        yield format_sse(name, payload)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def block_to_dict(block: Any) -> dict[str, Any]:
    """An SDK content block (or a dict) as a JSON-safe dict, ready to be stored
    and replayed verbatim. ``None`` fields are dropped; empty strings (a thinking
    block's omitted text) are kept."""
    if isinstance(block, dict):
        return json.loads(json.dumps(block))
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    return json.loads(json.dumps(vars(block), default=str))


def echo_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The blocks to store / replay. After a server-side fallback the API wants
    ``thinking`` / ``redacted_thinking`` / ``tool_use`` blocks BEFORE the last
    ``fallback`` marker left out (they belong to the model that declined); the
    marker itself is an audit record and is dropped. Whitespace-only text blocks
    (which the API refuses on replay) are dropped too."""
    last_fallback = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    out: list[dict[str, Any]] = []
    for i, b in enumerate(blocks):
        t = b.get("type")
        if t == "fallback":
            continue
        if i < last_fallback and t in ("thinking", "redacted_thinking", "tool_use", "server_tool_use"):
            continue
        if t == "text" and not str(b.get("text") or "").strip():
            continue
        out.append(b)
    return out


def usage_dict(usage: Any) -> dict[str, int]:
    def num(name: str) -> int:
        v = _get(usage, name)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    return {
        "input_tokens": num("input_tokens"), "output_tokens": num("output_tokens"),
        "cache_creation_input_tokens": num("cache_creation_input_tokens"),
        "cache_read_input_tokens": num("cache_read_input_tokens"),
    }


def classify_error(exc: BaseException) -> c.ErrorEvent:
    """Map a failed model call to a user-safe ``error`` event (raw provider text
    is logged, never sent)."""
    status = _get(exc, "status_code")
    name = type(exc).__name__
    if isinstance(exc, asyncio.TimeoutError) or "Timeout" in name:
        return c.ErrorEvent(code=ChatErrorCode.model_error.value, message="The assistant took too long to answer. Please try again.", retryable=True)
    if status == 429 or name == "RateLimitError":
        return c.ErrorEvent(code=ChatErrorCode.model_error.value, message="The assistant is busy right now. Please try again in a moment.", retryable=True)
    if (isinstance(status, int) and status >= 500) or name in ("APIConnectionError", "InternalServerError", "OverloadedError"):
        return c.ErrorEvent(code=ChatErrorCode.model_error.value, message="The assistant is temporarily unavailable. Please try again.", retryable=True)
    if isinstance(status, int) and status in (401, 403, 404):
        return c.ErrorEvent(code=ChatErrorCode.model_error.value, message="The assistant is not available right now.", retryable=False)
    if isinstance(status, int) and 400 <= status < 500:
        return c.ErrorEvent(code=ChatErrorCode.model_error.value, message="The assistant could not process this conversation.", retryable=False)
    return c.ErrorEvent(code=ChatErrorCode.internal.value, message="Something went wrong on our side. Please try again.", retryable=True)


def _card_evidence(card: c.Card) -> str:
    data = {k: v for k, v in card.data.items() if k not in _MONEY_KEYS}
    return json.dumps(data, ensure_ascii=False, default=str)


def _row_text(rec: MessageRecord) -> str:
    return str((rec.display or {}).get("text") or "")


# ---------------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------------


@dataclass
class TurnActor:
    user_id: str
    org_id: str


class _Turn:
    def __init__(
        self, *, session_id: str, actor: TurnActor, repo: ChatRepository, facades: ChatFacades,
        queue: "asyncio.Queue[Any]", client: Any | None, usage_recorder: UsageRecorder | None, turn_id: str,
    ):
        self.session_id = session_id
        self.actor = actor
        self.repo = repo
        self.facades = facades
        self.queue = queue
        self._client = client
        self._usage_recorder = usage_recorder
        self.turn_id = turn_id
        self.result = TurnResult(turn_id=turn_id)
        # filled by prepare()
        self.session: SessionRecord
        self.spec: DraftSpec
        self.user_text = ""
        self.tool_defs: list[dict[str, Any]] = []
        self.tool_result_texts: list[str] = []
        self.card_texts: list[str] = []
        self.user_texts: list[str] = []
        self.flag_numbers: list[str] = []
        self.pending_tool_ids: list[str] = []
        self.pending_message_id: str | None = None

    # -- events --------------------------------------------------------------
    def emit(self, name: str, payload: Any) -> None:
        try:
            self.queue.put_nowait((name, payload))
        except asyncio.QueueFull:  # the consumer is gone and the queue is bounded: persistence matters, delivery does not
            pass

    def emit_error(self, err: c.ErrorEvent) -> None:
        self.result.error = err
        self.emit(c.SseEventName.error.value, err)

    def emit_spec(self) -> None:
        self.emit(c.SseEventName.spec.value, c.SpecEvent(spec_version=self.spec.version, summary=self.spec.summary()))

    # -- client ----------------------------------------------------------------
    def client(self) -> Any:
        if self._client is None:
            if mock_enabled():
                from services.chat.mock_model import MockAnthropicClient

                self._client = MockAnthropicClient()
            else:
                from services.llm import get_async_client

                self._client = get_async_client()
        return self._client

    async def record_usage(self, model: str, final: Any) -> None:
        recorder = self._usage_recorder
        if recorder is None:
            from services.llm.usage import record_usage as recorder  # type: ignore[assignment]
        await recorder(
            org_id=self.actor.org_id, user_id=self.actor.user_id, feature="chat",
            resource_type="chat_session", resource_id=self.session_id, model=model,
            usage=_get(final, "usage"), request_id=_get(final, "_request_id"),
        )

    # -- setup -----------------------------------------------------------------
    async def prepare(self, text: str | None, resume: bool, client_turn_id: str | None) -> bool:
        session = await self.repo.get_session(self.session_id)
        if session is None:
            self.emit_error(c.ErrorEvent(code=ChatErrorCode.not_found.value, message="Chat not found.", retryable=False))
            return False
        self.session = session
        self.spec = load_spec(session.kind, session.spec)
        if isinstance(self.spec, EditSessionSpec) and not self.spec.protocol_id and session.protocol_id:
            self.spec.protocol_id = session.protocol_id
        rows = await self.repo.list_messages(self.session_id)

        if resume:
            convo = [r for r in rows if r.role not in (MessageRole.ui_action, MessageRole.system_note)]
            if not convo or convo[-1].role != MessageRole.user:
                self.emit_error(c.ErrorEvent(code=ChatErrorCode.invalid_action.value, message="There is no unanswered message to continue.", retryable=False))
                return False
            self.user_text = _row_text(convo[-1])
            if not session.title and self.user_text:
                await self.repo.update_session(self.session_id, title=self.user_text.replace("\n", " ")[:80].strip())
        else:
            cleaned = safety.clean_user_text(text or "", MAX_TURN_TEXT)
            if not cleaned:
                self.emit_error(c.ErrorEvent(code=ChatErrorCode.validation.value, message="The message was empty.", retryable=False))
                return False
            await self.repo.append_message(
                self.session_id, org_id=self.actor.org_id, role=MessageRole.user,
                api_content=[{"type": "text", "text": cleaned}],
                display={"text": cleaned, "client_turn_id": client_turn_id, "card_ids": [], "flags": {}},
                created_by=self.actor.user_id,
            )
            self.user_text = cleaned
            if not session.title:
                await self.repo.update_session(self.session_id, title=cleaned.replace("\n", " ")[:80].strip())
            rows = await self.repo.list_messages(self.session_id)

        # The per-turn snapshot of the draft, plus the card clicks since the last one.
        last_note = max((r.seq for r in rows if r.role == MessageRole.system_note), default=0)
        clicks = [_row_text(r) for r in rows if r.role == MessageRole.ui_action and r.seq > last_note]
        await self.repo.append_message(
            self.session_id, org_id=self.actor.org_id, role=MessageRole.system_note,
            api_content=prompts.render_draft_spec_message(self.spec, [x for x in clicks if x]), display={},
        )
        self.tool_defs = tools.tool_definitions(self.session.kind)
        self.user_texts = [_row_text(r) for r in rows if r.role == MessageRole.user]
        recent = (await self.repo.list_cards(self.session_id))[-_RECENT_CARDS_FOR_EVIDENCE:]
        self.card_texts = [_card_evidence(x) for x in recent]
        return True

    def evidence(self) -> list[str]:
        return [
            json.dumps(self.spec.to_json(), ensure_ascii=False, default=str),
            *self.user_texts, *self.card_texts, *self.tool_result_texts,
        ]

    # -- one model call ------------------------------------------------------------
    async def call_model(self, messages: list[dict[str, Any]], caps: Any) -> Any:
        namespace, params = build_request(
            model=caps.model, kind=self.session.kind, messages=messages, effort=chat_effort(),
            max_tokens=max_tokens_per_call(), server_fallbacks=caps.supports_server_fallbacks,
        )
        client = self.client()
        api = client.beta.messages if namespace == "beta" else client.messages
        async with api.stream(**params) as stream:
            async for event in stream:
                if _get(event, "type") == "content_block_delta":
                    delta = _get(event, "delta")
                    if _get(delta, "type") == "text_delta" and _get(delta, "text"):
                        self.emit(c.SseEventName.text_delta.value, c.TextDeltaEvent(text=str(_get(delta, "text"))))
            return await stream.get_final_message()

    # -- tools -----------------------------------------------------------------------
    async def run_tools(self, assistant_msg: MessageRecord, tool_uses: list[dict[str, Any]]) -> list[str]:
        """Execute every tool_use of one assistant message, persist ONE tool_results
        row, and return the ids of the cards produced."""
        self.pending_tool_ids = [str(t.get("id")) for t in tool_uses]
        self.pending_message_id = assistant_msg.id
        result_blocks: list[dict[str, Any]] = []
        card_ids: list[str] = []
        spec_dirty = False
        for tu in tool_uses:
            name = str(tu.get("name"))
            self.emit(c.SseEventName.tool_start.value, c.ToolStartEvent(tool=name, label=tools.tool_label(name)))
            ctx = tools.ToolContext(
                spec=self.spec, facades=self.facades, user_text=self.user_text, session_id=self.session_id,
            )
            try:
                outcome = await asyncio.wait_for(
                    tools.execute_tool(name, tu.get("input"), ctx), timeout=TOOL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                outcome = tools.ToolOutcome(content={"error": "The tool timed out. Try again."}, is_error=True)
            except Exception as exc:  # noqa: BLE001 - one broken tool must not break the turn
                _log_failure(f"chat tool {name} failed", exc)
                outcome = tools.ToolOutcome(content={"error": "The tool failed unexpectedly."}, is_error=True)
            text = outcome.content_text()
            self.tool_result_texts.append(text)
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tu.get("id"), "content": text}
            if outcome.is_error:
                block["is_error"] = True
            result_blocks.append(block)
            spec_dirty = spec_dirty or outcome.spec_changed
            for card in outcome.cards:
                await self.publish_card(card, assistant_msg.id)
                card_ids.append(card.id)
        if spec_dirty:
            await self.repo.save_spec(self.session_id, self.spec.to_json(), self.spec.version)
            self.emit_spec()
        await self.refresh_open_cards(exclude=set(card_ids))
        await self.repo.append_message(
            self.session_id, org_id=self.actor.org_id, role=MessageRole.tool_results,
            api_content=result_blocks, display={},
        )
        self.pending_tool_ids = []
        self.pending_message_id = None
        return card_ids

    async def publish_card(self, card: c.Card, message_id: str | None) -> None:
        if card.type in K.SUPERSEDING_TYPES:
            for old in await self.repo.supersede_cards(self.session_id, card.type, except_card_id=card.id):
                self.emit(c.SseEventName.card.value, old)
        await self.repo.upsert_card(self.session_id, org_id=self.actor.org_id, card=card, message_id=message_id)
        self.card_texts.append(_card_evidence(card))
        self.emit(c.SseEventName.card.value, card)

    async def refresh_open_cards(self, exclude: set[str]) -> None:
        """Re-render open cards whose content follows the spec (population,
        guideline, study-type options, ...) and publish only the ones that changed."""
        existing = await self.repo.list_cards(self.session_id)
        for fresh in K.refresh_cards(existing, self.spec, exclude):
            await self.repo.upsert_card(self.session_id, org_id=self.actor.org_id, card=fresh)
            self.emit(c.SseEventName.card.value, fresh)

    async def pair_unrun_tools(self, tool_uses: list[dict[str, Any]], reason: str) -> None:
        """Persist is_error tool_results for tool_use blocks that were NOT run, so
        the stored history stays valid for the next call."""
        if not tool_uses:
            return
        blocks = [
            {"type": "tool_result", "tool_use_id": tu.get("id"), "content": reason, "is_error": True}
            for tu in tool_uses
        ]
        await self.repo.append_message(
            self.session_id, org_id=self.actor.org_id, role=MessageRole.tool_results, api_content=blocks, display={},
        )

    # -- the loop -----------------------------------------------------------------------
    async def loop(self) -> None:
        from services.llm import capabilities

        caps = capabilities("chat")
        limit = max_iterations()
        last_msg: MessageRecord | None = None
        stop_reason = "end_turn"
        for iteration in range(1, limit + 1):
            await self.repo.touch_turn_lock(self.session_id, self.turn_id)
            rows = await self.repo.list_messages(self.session_id)
            messages = history_to_api_messages(rows, supports_system=caps.supports_mid_conversation_system)
            try:
                final = await self.call_model(messages, caps)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log_failure("chat model call failed", exc)
                self.emit_error(classify_error(exc))
                self.result.message_id = last_msg.id if last_msg else None
                self.result.stop_reason = "error"
                return
            self.result.model_calls += 1
            blocks = echo_blocks([block_to_dict(b) for b in (_get(final, "content") or [])])
            stop = str(_get(final, "stop_reason") or "end_turn")
            text = "".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")
            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            numbers = safety.find_unverified_numbers(text, self.evidence()) if text else []
            for n in numbers:
                if n not in self.flag_numbers:
                    self.flag_numbers.append(n)
            used_model = str(_get(final, "model") or caps.model)
            last_msg = await self.repo.append_message(
                self.session_id, org_id=self.actor.org_id, role=MessageRole.assistant,
                api_content=blocks or None,
                display={"text": text, "card_ids": [], "flags": {"unverified_numbers": numbers}},
                usage=usage_dict(_get(final, "usage")), model=used_model, stop_reason=stop,
            )
            try:
                await self.record_usage(used_model, final)
            except Exception as exc:  # noqa: BLE001 - the ledger never breaks a turn
                _log_failure("usage recording failed", exc)
            self.result.message_id = last_msg.id
            stop_reason = stop

            if stop == "refusal":
                await self.pair_unrun_tools(tool_uses, "Not run: the response was declined.")
                await self.handle_refusal(last_msg, final)
                break
            if stop == "max_tokens":
                await self.pair_unrun_tools(tool_uses, "Not run: the response was cut off before the tool call finished.")
                self.emit(c.SseEventName.notice.value, c.NoticeEvent(level="info", text="That reply was cut off because it got too long. Please ask again, or break the request into smaller steps."))
                break
            if tool_uses:
                card_ids = await self.run_tools(last_msg, tool_uses)
                if card_ids:
                    d = dict(last_msg.display or {})
                    d["card_ids"] = card_ids
                    await self.repo.set_message_display(last_msg.id, d)
                if iteration >= limit:
                    stop_reason = "iteration_limit"
                    self.emit(c.SseEventName.notice.value, c.NoticeEvent(level="info", text="I reached the step limit for one message. The cards above show what I did; tell me what to do next."))
                    break
                continue
            if stop == "pause_turn":
                if iteration >= limit:
                    stop_reason = "iteration_limit"
                    break
                continue
            break
        self.result.stop_reason = stop_reason
        self.result.flags = MessageFlags(unverified_numbers=list(self.flag_numbers))

    async def handle_refusal(self, msg: MessageRecord, final: Any) -> None:
        details = _get(final, "stop_details")
        category = _get(details, "category")
        text = "The assistant declined to continue with that request. You can rephrase it, or carry on with the cards."
        card = K.build_notice_card(self.spec.version, "warning", text, code=str(category) if category else "refusal")
        await self.publish_card(card, msg.id)
        d = dict(msg.display or {})
        d["card_ids"] = [card.id]
        await self.repo.set_message_display(msg.id, d)

    # -- cancellation -----------------------------------------------------------------------
    async def settle_interrupted_tools(self) -> None:
        """If the turn was cancelled between persisting a tool_use message and its
        tool_results, close the pair so the history stays valid."""
        if self.pending_tool_ids:
            ids, self.pending_tool_ids = self.pending_tool_ids, []
            await self.pair_unrun_tools([{"id": i} for i in ids], "Not run: the request was interrupted.")


async def run_turn(
    *,
    session_id: str,
    actor: TurnActor,
    repo: ChatRepository,
    facades: ChatFacades,
    queue: "asyncio.Queue[Any]",
    text: str | None = None,
    resume: bool = False,
    client_turn_id: str | None = None,
    turn_id: str | None = None,
    client: Any | None = None,
    usage_recorder: UsageRecorder | None = None,
) -> TurnResult:
    """Run one turn and stream its events into ``queue``.

    PRECONDITIONS (the router's job, before opening the stream): the caller is
    authorised for the session and has passed the role / license / rate-limit /
    daily and per-session caps; it has called ``repo.acquire_turn_lock(session_id,
    turn_id)`` (409 ``busy`` if that returns False) and passes the same ``turn_id``.
    ``run_turn`` always releases that lock and always ends by putting ``None`` on
    the queue. It never raises (other than ``asyncio.CancelledError``); failures
    become an ``error`` event and ``TurnResult.error``.

    Exactly one of ``text`` (a new user message) or ``resume=True`` (answer the
    stored unanswered user message) is expected. ``client`` is injectable; when
    omitted it is the mock client under ``MOCK_AI`` and the provider client
    otherwise."""
    turn_id = turn_id or uuid.uuid4().hex
    turn = _Turn(
        session_id=session_id, actor=actor, repo=repo, facades=facades, queue=queue, client=client,
        usage_recorder=usage_recorder, turn_id=turn_id,
    )
    try:
        if await turn.prepare(text, resume, client_turn_id):
            turn.emit(c.SseEventName.turn_start.value, c.TurnStartEvent(turn_id=turn_id))
            await turn.loop()
            if turn.result.error is None and turn.result.message_id:
                turn.emit(
                    c.SseEventName.turn_end.value,
                    c.TurnEndEvent(
                        message_id=turn.result.message_id, stop_reason=turn.result.stop_reason,
                        flags=turn.result.flags,
                    ),
                )
    except asyncio.CancelledError:
        await asyncio.shield(turn.settle_interrupted_tools())
        raise
    except Exception as exc:  # noqa: BLE001
        _log_failure("chat turn failed", exc)
        turn.emit_error(c.ErrorEvent(code=ChatErrorCode.internal.value, message="Something went wrong on our side. Please try again.", retryable=True))
    finally:
        try:
            await asyncio.shield(repo.release_turn_lock(session_id, turn_id))
        except Exception as exc:  # noqa: BLE001
            _log_failure("could not release the chat turn lock", exc)
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover
            pass
    return turn.result
