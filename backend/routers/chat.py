"""Chat API (docs/redesign-chat-plan.md B3, B8, B9): conversational protocol /
budget / protocol-edit sessions.

ROUTES (no trailing slash, so a cross-origin 307 can never drop Authorization):

    POST   /api/chat                          create a session (no model call)
    GET    /api/chat?kind=&limit=             my sessions
    GET    /api/chat/stream-check             SSE proof that streams are not buffered (no AI)
    GET    /api/chat/{sid}                    transcript, cards, spec summary
    PATCH  /api/chat/{sid}                    rename / archive
    POST   /api/chat/{sid}/turns              one AI turn, streamed (text/event-stream)
    POST   /api/chat/{sid}/actions            a card click, deterministic, no AI
    GET    /api/chat/{sid}/proposals/{pid}    poll an edit proposal

``/stream-check`` is declared BEFORE ``/{sid}`` so the literal path wins.

GATES (all HTTP errors carry ``detail`` as ``{"code", "message"}`` from
``contract.error_detail``, or the existing plain-string wording for the 402
license / role 403s):

* every route needs a session (``require_user``) and resolves the verified actor;
* a transcript is visible ONLY to its creator (user decision 2026-10-23), on top of
  the org check (``dependencies.get_chat_session_scoped``);
* protocol and protocol_edit chats need the generator role; budget chats need no
  role (budgets are not a signed regulatory artifact);
* ``POST /api/chat`` has NO license check, so a pending-license org can save its
  landing-page prompt (no AI is involved); turns need an active license, the
  per-minute limiter, the per-session and per-day caps, and the turn lock;
* actions: 409 ``busy`` while a turn is running, 409 ``stale_spec`` when the
  client's spec is behind, and the credit-spending ones (generate, confirm an
  edit) need an active license.

THE TURN RUNS OUTSIDE THE REQUEST. FastAPI 0.111 closes ``Depends(get_db)``
sessions before a streaming body runs, and a browser disconnect must not stop a
turn (every ``tool_use`` needs its ``tool_result``). So after the gates pass the
turn is an ``asyncio`` task (a strong reference is kept) that writes SSE events
into a queue with its OWN ``database.AsyncSessionLocal()`` sessions; the response
just drains the queue.

The turn lock doubles as the mutex for actions (one writer to a session's spec at
a time), so an action also gets 409 ``busy`` while another action or a turn runs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_user
from database import get_db
from dependencies import (
    audit_logger,
    authenticate_actor as _authenticate_actor,
    get_chat_session_scoped,
    get_protocol_scoped,
    rate_limit,
    require_active_license,
)
from models import AuditLog, Drug, Protocol, ProtocolStatus, User, UserRole
from routers.budgets import create_budget_for_actor
from routers.protocols import _require_role, start_protocol_generation
from schemas import BudgetCreate, ProtocolCreate
from services.chat import cards as K
from services.chat import contract as c
from services.chat import edits, safety
from services.chat.facades import FacadeError, RealFacades, drug_row
from services.chat.orchestrator import TurnActor, iter_sse_frames, run_turn
from services.chat.spec import Actor, BudgetDraftSpec, EditSessionSpec, load_spec, new_spec
from services.chat.store import (
    SessionRecord,
    SqlChatRepository,
    awaiting_reply,
    message_display,
    session_read,
)

logger = logging.getLogger(__name__)


def _log_failure(what: str, exc: BaseException) -> None:
    """Log WITHOUT the traceback at error level. Transcripts must not reach Sentry
    (B7): its logging integration attaches stack-frame locals (user text, tool
    results) to exception events, so only the class name is logged at error level
    and the full traceback only at DEBUG."""
    logger.error("%s: %s", what, type(exc).__name__)
    logger.debug("%s (traceback)", what, exc_info=True)


router = APIRouter(
    prefix="/api/chat",
    tags=["chat"],
    dependencies=[Depends(require_user), Depends(audit_logger)],
)

#: One stateless repository: every call opens its own session (store.py).
repo = SqlChatRepository()

#: Strong references to in-flight turn tasks (asyncio keeps only weak ones).
_TURN_TASKS: set[asyncio.Task] = set()

#: Read at call time so tests can shorten them.
STREAM_CHECK_INTERVAL_SECONDS = c.STREAM_CHECK_INTERVAL_SECONDS

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}
_PROTOCOL_KINDS = frozenset({c.SessionKind.protocol, c.SessionKind.protocol_edit})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _err(code: c.ChatErrorCode, message: str, *, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(c.ERROR_HTTP_STATUS[code], detail=c.error_detail(code, message), headers=headers)


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except ValueError:
        return default


def session_turn_cap() -> int:
    return _int_env("CHAT_SESSION_TURN_CAP", c.CHAT_SESSION_TURN_CAP)


def daily_turn_cap() -> int:
    return _int_env("CHAT_DAILY_TURN_CAP", c.CHAT_DAILY_TURN_CAP)


def heartbeat_seconds() -> float:
    try:
        return max(0.05, float(os.getenv("CHAT_SSE_HEARTBEAT_SECONDS", c.SSE_HEARTBEAT_SECONDS)))
    except ValueError:
        return float(c.SSE_HEARTBEAT_SECONDS)


def _record(row: Any) -> SessionRecord:
    return SqlChatRepository._session_record(row)


def _require_kind_role(actor: User, kind: c.SessionKind) -> None:
    """Protocol kinds are Author (generator) work; budgets have no role gate."""
    if kind in _PROTOCOL_KINDS:
        _require_role(actor, {UserRole.generator}, action="start or continue protocol chats")


def _facades(actor: User) -> RealFacades:
    # Resolve the actor fresh inside each call's own session (no ORM object is
    # shared between the request session and the facades).
    return RealFacades(actor_id=str(actor.id), org_id=actor.org_id)


def _protocol_option(protocol: Protocol) -> dict[str, Any]:
    return {
        "id": str(protocol.id),
        "drug_inn": protocol.drug.inn if protocol.drug else "",
        "study_type": protocol.study_type.value,
        "status": protocol.status.value,
        "version": int(protocol.version),
        "created_at": protocol.created_at.isoformat() if protocol.created_at else None,
    }


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _card_uuid(kind: str, key: str) -> str:
    """A stable card id, so materialising the same card twice is idempotent."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"quarkclinical:chat-card:{kind}:{key}"))


def _merge_cards(*groups: list[c.Card]) -> list[c.Card]:
    """Cards by id, first-seen order, the LATEST version of each winning."""
    out: dict[str, c.Card] = {}
    for group in groups:
        for card in group:
            out[card.id] = card
    return list(out.values())


async def _announce(rec: SessionRecord, text: str, cards: list[c.Card]) -> None:
    """Append a card-carrying transcript line that is NOT part of the model's
    history (a ``ui_action`` row has no ``api_content``), and store ``cards`` on it."""
    msg = await repo.append_message(
        rec.id, org_id=rec.org_id, role=c.MessageRole.ui_action, api_content=None,
        display={"text": text, "card_ids": [x.id for x in cards], "flags": {}},
    )
    for card in cards:
        try:
            await repo.upsert_card(rec.id, org_id=rec.org_id, card=card, message_id=msg.id)
        except IntegrityError:  # a concurrent refresh created the same stable card
            logger.debug("card %s already stored", card.id)


# ---------------------------------------------------------------------------
# POST /api/chat, GET /api/chat
# ---------------------------------------------------------------------------


@router.post("", response_model=c.ChatSessionRead, status_code=201)
async def create_chat(payload: c.CreateChatRequest, request: Request, db: AsyncSession = Depends(get_db)):
    actor = await _authenticate_actor(request, db)
    kind = payload.kind
    _require_kind_role(actor, kind)

    protocol: Protocol | None = None
    if payload.protocol_id is not None:
        # 404 unknown / 403 other org: a guessed id never opens another org's protocol.
        protocol = await get_protocol_scoped(db, uuid.UUID(payload.protocol_id), actor, load_drug=True)
    if kind == c.SessionKind.protocol_edit:
        assert protocol is not None  # CreateChatRequest guarantees protocol_id
        if protocol.status != ProtocolStatus.draft:
            raise _err(
                c.ChatErrorCode.invalid_action,
                f"This protocol is '{protocol.status.value}'. Chat edits only work on draft protocols; "
                "clone it to start a new draft.",
            )

    facades = _facades(actor)
    title: str | None = None
    if kind == c.SessionKind.protocol_edit and protocol is not None:
        spec = new_spec(kind, protocol_id=str(protocol.id))
        assert isinstance(spec, EditSessionSpec)
        snapshot = await facades.get_protocol(str(protocol.id))
        if snapshot is not None:
            spec.load_overview(snapshot)
        title = f"Edit {protocol.drug.inn if protocol.drug else 'protocol'}"[:80]
    elif kind == c.SessionKind.budget:
        spec = new_spec(kind)
        assert isinstance(spec, BudgetDraftSpec)
        if protocol is not None:
            spec.set_protocol(_protocol_option(protocol), actor=Actor.click)
    else:
        spec = new_spec(kind)

    first = safety.clean_user_text(payload.initial_message or "", c.MAX_INITIAL_MESSAGE)
    if first and title is None:
        title = first.replace("\n", " ")[:80].strip() or None

    from services.llm import model_id

    rec = await repo.create_session(
        org_id=actor.org_id, created_by=str(actor.id), kind=kind, title=title,
        protocol_id=str(protocol.id) if protocol is not None else None,
        spec=spec.to_json(), spec_version=spec.version, model=model_id("chat"),
    )
    request.state.audit_resource_id = rec.id
    if first:
        # Stored UNPROCESSED: no model call is made here, so a pending-license org
        # can save its landing prompt; the client sends a turn with resume=true later.
        await repo.append_message(
            rec.id, org_id=actor.org_id, role=c.MessageRole.user,
            api_content=[{"type": "text", "text": first}],
            display={"text": first, "client_turn_id": None, "card_ids": [], "flags": {}},
            created_by=str(actor.id),
        )
    return session_read(rec)


@router.get("", response_model=list[c.ChatSessionListItem])
async def list_chats(
    request: Request,
    kind: c.SessionKind | None = None,
    limit: int = Query(c.DEFAULT_LIST_LIMIT, ge=1, le=c.MAX_LIST_LIMIT),
    db: AsyncSession = Depends(get_db),
):
    actor = await _authenticate_actor(request, db)
    rows = await repo.list_sessions(org_id=actor.org_id, created_by=str(actor.id), kind=kind, limit=limit)
    return [c.ChatSessionListItem.model_validate(r, from_attributes=True) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/chat/stream-check  (declared before /{sid})
# ---------------------------------------------------------------------------


@router.get("/stream-check")
async def stream_check():
    """A short SSE test with no AI: ``STREAM_CHECK_EVENTS`` ``tick`` events spaced
    ``STREAM_CHECK_INTERVAL_SECONDS`` apart. Deployed behind a proxy, the browser
    seeing the ticks arrive one at a time proves the stream is not buffered."""

    async def gen() -> AsyncIterator[str]:
        started = time.monotonic()
        total = c.STREAM_CHECK_EVENTS
        for seq in range(1, total + 1):
            if seq > 1:
                await asyncio.sleep(STREAM_CHECK_INTERVAL_SECONDS)
            yield c.format_sse(
                c.SseEventName.tick.value,
                c.StreamCheckTick(seq=seq, total=total, elapsed_ms=int((time.monotonic() - started) * 1000)),
            )

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ---------------------------------------------------------------------------
# GET /api/chat/{sid}
# ---------------------------------------------------------------------------


async def _sync_generation_cards(db: AsyncSession, rec: SessionRecord, cards: list[c.Card]) -> list[c.Card]:
    """Bring generation_progress cards in line with ``protocol.generation_status``;
    when a protocol finished, add its protocol_ready card (once)."""
    out = list(cards)
    ready_for = {x.data.get("protocol_id") for x in cards if x.type == c.CardType.protocol_ready}
    for i, card in enumerate(cards):
        if card.type != c.CardType.generation_progress or card.status != c.CardStatus.active:
            continue
        pid = _uuid_or_none(card.data.get("protocol_id"))
        if pid is None:
            continue
        protocol = (await db.execute(
            select(Protocol).where(Protocol.id == pid)
        )).scalar_one_or_none()
        if protocol is None or protocol.org_id != rec.org_id:
            continue
        status = protocol.generation_status or "complete"
        if status not in ("complete", "failed"):
            continue
        drug_inn = card.data.get("drug_inn")
        fresh = K.build_generation_progress_card(
            card.spec_version, str(pid), drug_inn, status, protocol.generation_error if status == "failed" else None,
            card_id=card.id,
        )
        await repo.upsert_card(rec.id, org_id=rec.org_id, card=fresh)
        out[i] = fresh
        if status == "complete" and str(pid) not in ready_for:
            ready = _ready_card(rec.spec_version, protocol, drug_inn)
            await _announce(rec, "Your protocol is ready.", [ready])
            out.append(ready)
            ready_for.add(str(pid))
    return out


def _ready_card(spec_version: int, protocol: Protocol, drug_inn: str | None) -> c.Card:
    card = K.build_protocol_ready_card(spec_version, {
        "id": str(protocol.id), "drug_inn": drug_inn, "study_type": protocol.study_type.value,
        "guideline_version": protocol.guideline_version, "sections": dict(protocol.sections or {}),
        "version": int(protocol.version),
    })
    return card.model_copy(update={"id": _card_uuid("protocol_ready", str(protocol.id))})


async def _sync_edit_cards(db: AsyncSession, rec: SessionRecord, cards: list[c.Card]) -> list[c.Card]:
    """Bring edit_progress / edit_diff cards in line with their proposals, and add
    the edit_diff card (once) when a proposal becomes ready. A proposal stuck in
    ``generating`` for more than 30 minutes is marked failed here."""
    out = list(cards)
    have_diff = {x.data.get("proposal_id") for x in cards if x.type == c.CardType.edit_diff}
    for i, card in enumerate(cards):
        if card.status != c.CardStatus.active or card.type not in (c.CardType.edit_progress, c.CardType.edit_diff):
            continue
        proposal = await edits.load_proposal(db, card.data.get("proposal_id"), org_id=rec.org_id, session_id=rec.id)
        if proposal is None:
            continue
        await edits.expire_if_stuck(db, proposal)
        protocol = (await db.execute(select(Protocol).where(Protocol.id == proposal.protocol_id))).scalar_one_or_none()
        await edits.refresh_staleness(db, proposal, protocol)
        status = proposal.status.value
        if card.type == c.CardType.edit_progress:
            if status == "generating":
                continue
            fresh = K.build_edit_progress_card(card.spec_version, str(proposal.id), proposal.kind.value, status, proposal.error)
            fresh = fresh.model_copy(update={"id": card.id})
            await repo.upsert_card(rec.id, org_id=rec.org_id, card=fresh)
            out[i] = fresh
            if status == "ready" and str(proposal.id) not in have_diff:
                view = await edits.build_view(db, proposal, protocol)
                diff = K.build_edit_diff_card(rec.spec_version, view)
                diff = diff.model_copy(update={"id": _card_uuid("edit_diff", str(proposal.id))})
                await _announce(rec, "The proposed changes are ready to review.", [diff])
                out.append(diff)
                have_diff.add(str(proposal.id))
        else:  # edit_diff
            if status in ("applied", "discarded"):
                done = K.with_status(card, c.CardStatus.done, data_patch={"status": status})
                done = done.model_copy(update={"actions": []})
                await repo.upsert_card(rec.id, org_id=rec.org_id, card=done)
                out[i] = done
            elif status in ("stale", "failed"):
                dead = K.with_status(card, c.CardStatus.superseded, data_patch={"status": status, "stale": True})
                dead = dead.model_copy(update={"actions": []})
                await repo.upsert_card(rec.id, org_id=rec.org_id, card=dead)
                out[i] = dead
    return out


async def _synced_cards(db: AsyncSession, rec: SessionRecord) -> list[c.Card]:
    """The session's cards, refreshed from the protocol / proposal state. A failed
    refresh (e.g. a race with a concurrent read creating the same card) must never
    take the transcript down: fall back to the stored cards."""
    cards = await repo.list_cards(rec.id)
    try:
        if any(x.type == c.CardType.generation_progress and x.status == c.CardStatus.active for x in cards):
            cards = await _sync_generation_cards(db, rec, cards)
        if rec.kind == c.SessionKind.protocol_edit and any(
            x.type in (c.CardType.edit_progress, c.CardType.edit_diff) and x.status == c.CardStatus.active
            for x in cards
        ):
            cards = await _sync_edit_cards(db, rec, cards)
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        _log_failure("could not refresh the chat cards", exc)
        cards = await repo.list_cards(rec.id)
    return cards


@router.get("/{sid}", response_model=c.ChatSessionDetail)
async def get_chat(sid: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    actor = await _authenticate_actor(request, db)
    row = await get_chat_session_scoped(db, sid, actor)
    rec = _record(row)
    cards = await _synced_cards(db, rec)
    rows = await repo.list_messages(rec.id)
    messages = [m for m in (message_display(r) for r in rows) if m is not None]
    spec = load_spec(rec.kind, rec.spec)
    return c.ChatSessionDetail(
        session=session_read(rec), messages=messages, cards=cards, spec=spec.summary(),
        awaiting_reply=awaiting_reply(rows),
    )


# ---------------------------------------------------------------------------
# PATCH /api/chat/{sid}
# ---------------------------------------------------------------------------


@router.patch("/{sid}", response_model=c.ChatSessionRead)
async def patch_chat(sid: uuid.UUID, payload: c.PatchChatRequest, request: Request, db: AsyncSession = Depends(get_db)):
    actor = await _authenticate_actor(request, db)
    await get_chat_session_scoped(db, sid, actor)
    fields: dict[str, Any] = {}
    if payload.title is not None:
        title = safety.clean_free_text(payload.title, c.MAX_TITLE)
        if not title:
            raise _err(c.ChatErrorCode.validation, "The title was empty.")
        fields["title"] = title
    if payload.status is not None:
        fields["status"] = payload.status
    await repo.update_session(str(sid), **fields)
    rec = await repo.get_session(str(sid))
    assert rec is not None
    return session_read(rec)


# ---------------------------------------------------------------------------
# POST /api/chat/{sid}/turns  (SSE)
# ---------------------------------------------------------------------------


async def _run_turn_task(**kwargs: Any) -> None:
    """The turn. ``run_turn`` never raises, always releases the lock and always
    ends the queue; this wrapper only guards against a failure before it starts."""
    queue: asyncio.Queue = kwargs["queue"]
    try:
        await run_turn(**kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        _log_failure("chat turn task crashed", exc)
        try:
            await repo.release_turn_lock(kwargs["session_id"], kwargs["turn_id"])
        finally:
            queue.put_nowait(None)


def _replay_stream(turn_id: str, assistant_row: Any) -> StreamingResponse:
    """An idempotent retry of a turn that already has its answer: no second run,
    just the closing events so the client can stop waiting."""
    flags = c.MessageFlags(
        unverified_numbers=[str(x) for x in (((assistant_row.display or {}).get("flags") or {}).get("unverified_numbers") or [])]
    )

    async def gen() -> AsyncIterator[str]:
        yield c.format_sse(c.SseEventName.turn_start.value, c.TurnStartEvent(turn_id=turn_id))
        yield c.format_sse(
            c.SseEventName.turn_end.value,
            c.TurnEndEvent(message_id=assistant_row.id, stop_reason=assistant_row.stop_reason or "end_turn", flags=flags),
        )

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post(
    "/{sid}/turns",
    dependencies=[Depends(rate_limit(20, 60, scope="chat_turn"))],
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def post_turn(sid: uuid.UUID, payload: c.TurnRequest, request: Request, db: AsyncSession = Depends(get_db)):
    actor = await _authenticate_actor(request, db)
    row = await get_chat_session_scoped(db, sid, actor)
    rec = _record(row)
    _require_kind_role(actor, rec.kind)
    await require_active_license(db, actor)
    session_id = rec.id
    resume = payload.resume

    # Idempotency: the same client_turn_id never runs a turn twice.
    if not resume:
        prior = await repo.find_user_message_by_client_turn(session_id, payload.client_turn_id)
        if prior is not None:
            if await repo.is_turn_locked(session_id):
                raise _err(c.ChatErrorCode.busy, "This message is still being answered.")
            rows = await repo.list_messages(session_id)
            answers = [r for r in rows if r.seq > prior.seq and r.role == c.MessageRole.assistant]
            if answers:
                return _replay_stream(uuid.uuid4().hex, answers[-1])
            resume = True  # stored but never answered: answer it rather than store it twice

    # Caps. A resumed message is already counted, so it may sit AT the cap.
    session_count = await repo.count_user_messages(session_id)
    if session_count >= session_turn_cap() + (1 if resume else 0):
        raise _err(
            c.ChatErrorCode.session_cap,
            f"This chat has reached its limit of {session_turn_cap()} messages. Start a new chat to continue.",
        )
    day_count = await repo.count_user_messages_since(str(actor.id), datetime.now(timezone.utc) - timedelta(hours=24))
    if day_count >= daily_turn_cap() + (1 if resume else 0):
        raise _err(
            c.ChatErrorCode.daily_cap,
            "You have reached today's limit for assistant messages. Card buttons still work; try again tomorrow.",
        )

    # The lock is taken BEFORE the stream opens so a busy session is a plain 409.
    turn_id = uuid.uuid4().hex
    if not await repo.acquire_turn_lock(session_id, turn_id):
        raise _err(c.ChatErrorCode.busy, "The assistant is still answering the previous message.")

    queue: asyncio.Queue = asyncio.Queue()
    try:
        task = asyncio.create_task(_run_turn_task(
            session_id=session_id, actor=TurnActor(user_id=str(actor.id), org_id=actor.org_id), repo=repo,
            facades=_facades(actor), queue=queue, text=None if resume else payload.text, resume=resume,
            client_turn_id=payload.client_turn_id, turn_id=turn_id,
        ))
    except BaseException:
        await repo.release_turn_lock(session_id, turn_id)
        raise
    _TURN_TASKS.add(task)
    task.add_done_callback(_TURN_TASKS.discard)
    return StreamingResponse(
        iter_sse_frames(queue, heartbeat_seconds()), media_type="text/event-stream", headers=_SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# POST /api/chat/{sid}/actions
# ---------------------------------------------------------------------------


def _click_label(payload: c.ActionRequest, clicked: c.Card | None, spec: Any) -> str:
    """The transcript line (and the model's next-turn hint) for a card click."""
    A = c.ActionType
    p = payload.payload
    a = payload.action
    button = next((x.label for x in (clicked.actions if clicked else []) if x.id == a.value), None)
    if a == A.select_drug:
        drug = getattr(spec, "drug", None)
        return f"Selected drug: {drug.inn}" if drug else "Selected a drug"
    if a == A.add_drug:
        return f"Added '{p.get('inn')}' to the drug catalogue and selected it"
    if a == A.set_study_type:
        return f"Chose study type: {p.get('study_type')}"
    if a == A.set_guideline:
        return f"Chose guideline: {p.get('guideline_version')}"
    if a == A.update_population:
        keys = ", ".join(sorted((p.get("patch") or {}).keys()))
        return f"Saved participants: {keys}" if keys else "Saved participants"
    if a == A.toggle_exclusion:
        return f"{'Added' if p.get('on') else 'Removed'} exclusion criterion: {p.get('text')}"
    if a == A.set_design_decision:
        return f"Set design decision {p.get('key')} to: {p.get('value')}"
    if a == A.set_indication:
        return f"Chose indication: {p['text']}" if p.get("text") else "Let the engine choose the indication"
    if a == A.confirm_design:
        return "Confirmed the study design"
    if a == A.generate_protocol:
        return button or "Clicked Generate protocol"
    if a == A.set_budget_fields:
        keys = ", ".join(sorted((p.get("patch") or {}).keys()))
        return f"Updated budget fields: {keys}"
    if a == A.link_protocol:
        return "Linked a protocol to the budget" if p.get("protocol_id") else "Unlinked the protocol from the budget"
    if a == A.create_budget:
        return button or "Clicked Create budget"
    if a == A.confirm_edit:
        return button or "Confirmed the change request"
    if a == A.apply_edit:
        return button or "Applied the proposed changes"
    return button or "Discarded the change request"


async def _add_drug(db: AsyncSession, actor: User, inn: str, session_id: str) -> dict[str, Any]:
    """Add ``inn`` to the global catalogue (the confirm-card click), mirroring
    ``POST /api/drugs/`` -- except that an INN that already exists is simply
    selected, not a 409. Returns the catalogue row."""
    inn = inn.strip().lower()
    existing = (await db.execute(select(Drug).where(Drug.inn == inn))).scalar_one_or_none()
    if existing is not None:
        return drug_row(existing)
    drug = Drug(id=uuid.uuid4(), inn=inn, brand_names=[], rxnorm_id=None)
    db.add(drug)
    db.add(AuditLog(
        id=uuid.uuid4(), user_id=actor.id, action="CHAT_CREATE:drug", resource_type="drug", resource_id=str(drug.id),
        diff={"inn": inn, "chat_session_id": session_id}, timestamp=datetime.now(timezone.utc),
    ))
    await db.commit()
    return drug_row(drug)


def _ready_view_card(spec_version: int, view: dict[str, Any]) -> c.Card:
    card = K.build_edit_diff_card(spec_version, view)
    return card.model_copy(update={"id": _card_uuid("edit_diff", str(view["id"]))})


async def _execute_action(
    db: AsyncSession, request: Request, actor: User, rec: SessionRecord, payload: c.ActionRequest,
) -> c.ActionResponse:
    session_id = rec.id
    spec = load_spec(rec.kind, rec.spec)
    if payload.spec_version != spec.version:
        raise _err(
            c.ChatErrorCode.stale_spec,
            "This card is out of date: the draft changed since it was shown. Refresh to see the latest.",
        )
    existing = await repo.list_cards(session_id)
    facades = _facades(actor)
    try:
        outcome = await K.apply_action(spec, payload, facades, session_id=session_id, existing_cards=existing)
    except K.ActionError as exc:
        raise _err(exc.code, exc.message) from None
    except FacadeError as exc:
        raise HTTPException(exc.status_code, detail=exc.message) from None

    intent = outcome.intent
    final_spec = outcome.spec
    guide = outcome.guide
    changed: list[c.Card] = list(outcome.cards)
    clicked_id = str(payload.card_id)

    try:
        if isinstance(intent, K.AddDrugIntent):
            drug = await _add_drug(db, actor, intent.inn, session_id)
            select_req = c.ActionRequest(
                card_id=payload.card_id, action=c.ActionType.select_drug,
                payload={"drug_id": drug["id"]}, spec_version=payload.spec_version,
            )
            outcome = await K.apply_action(spec, select_req, facades, session_id=session_id, existing_cards=existing)
            final_spec, guide, changed = outcome.spec, outcome.guide, list(outcome.cards)

        elif isinstance(intent, K.GenerateProtocolIntent):
            body = ProtocolCreate.model_validate(intent.payload)
            # request=None: the audit row stays attached to the chat session.
            protocol = await start_protocol_generation(db, None, actor, body, provenance=intent.provenance)
            info = {
                "id": str(protocol.id), "drug_inn": final_spec.drug.inn if final_spec.drug else None,  # type: ignore[union-attr]
                "generation_status": protocol.generation_status, "generation_error": protocol.generation_error,
            }
            after = K.after_generation_started(final_spec, info, clicked_card_id=clicked_id, existing=existing)
            final_spec, guide = after.spec, after.guide
            changed = _merge_cards(changed, after.cards)
            await repo.update_session(session_id, protocol_id=str(protocol.id))
            # request=None above keeps the audit row on the chat session, so the
            # protocol's own trail gets an explicit entry (best effort, like audit_logger).
            try:
                db.add(AuditLog(
                    id=uuid.uuid4(), user_id=actor.id, action="CHAT_CREATE:protocol", resource_type="protocol",
                    resource_id=str(protocol.id), timestamp=datetime.now(timezone.utc),
                    diff={
                        "chat_session_id": session_id, "spec_version": intent.provenance.get("spec_version"),
                        "study_type": body.study_type.value, "guideline_version": body.guideline_version,
                    },
                ))
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                _log_failure("could not write the CHAT_CREATE:protocol audit row", exc)
            if protocol.generation_status == "complete":
                changed = _merge_cards(changed, [_ready_card(final_spec.version, protocol, info["drug_inn"])])
                guide = c.Guide(text="Your protocol is ready. Open it in the editor, or price this study.", chips=[])

        elif isinstance(intent, K.CreateBudgetIntent):
            body = BudgetCreate.model_validate(intent.payload)
            budget = await create_budget_for_actor(db, None, actor, body)
            budget_dict = budget.model_dump(mode="json")
            db.add(AuditLog(
                id=uuid.uuid4(), user_id=actor.id, action="CHAT_CREATE:budget", resource_type="budget",
                resource_id=str(budget.id), diff={**intent.provenance, "chat_session_id": session_id},
                timestamp=datetime.now(timezone.utc),
            ))
            await db.commit()
            after = K.after_budget_created(final_spec, budget_dict, clicked_card_id=clicked_id, existing=existing)  # type: ignore[arg-type]
            final_spec, guide = after.spec, after.guide
            changed = _merge_cards(changed, after.cards)
            await repo.update_session(session_id, budget_id=str(budget.id))

        elif isinstance(intent, K.ConfirmEditIntent):
            final_spec, guide, changed = await _confirm_edit(db, actor, rec, intent, final_spec, changed, existing, clicked_id)

        elif isinstance(intent, K.ApplyEditIntent):
            try:
                view = await edits.apply_proposal(db, proposal_id=intent.proposal_id, session_id=session_id, actor=actor)
            except edits.ProposalError as exc:
                if exc.code == c.ChatErrorCode.proposal_stale:
                    await _supersede_diff_cards(rec, existing, intent.proposal_id)
                raise _err(exc.code, exc.message) from None
            after = K.after_edit_applied(final_spec, view, clicked_card_id=clicked_id, existing=existing)  # type: ignore[arg-type]
            final_spec, guide = after.spec, after.guide
            changed = _merge_cards(changed, _supersede_for_new(after.cards, existing), after.cards)

        elif isinstance(intent, K.DiscardEditIntent):
            if not intent.pending_only:
                try:
                    await edits.discard_proposal(db, proposal_id=intent.proposal_id, session_id=session_id, actor=actor)
                except edits.ProposalError as exc:
                    raise _err(exc.code, exc.message) from None
            after = K.after_edit_discarded(final_spec, clicked_card_id=clicked_id, existing=existing)  # type: ignore[arg-type]
            final_spec, guide = after.spec, after.guide
            changed = _merge_cards(changed, after.cards)
    except FacadeError as exc:
        raise HTTPException(exc.status_code, detail=exc.message) from None
    except K.ActionError as exc:
        raise _err(exc.code, exc.message) from None

    # ---- persist: transcript line, cards, then the spec ---------------------------
    existing_ids = {x.id for x in existing}
    new_cards = [x for x in changed if x.id not in existing_ids]
    clicked = next((x for x in existing if x.id == clicked_id), None)
    msg = await repo.append_message(
        session_id, org_id=rec.org_id, role=c.MessageRole.ui_action, api_content=None,
        display={
            "text": safety.clean_free_text(_click_label(payload, clicked, final_spec), 300),
            "card_ids": [x.id for x in new_cards], "flags": {},
        },
        created_by=str(actor.id),
    )
    for card in changed:
        await repo.upsert_card(
            session_id, org_id=rec.org_id, card=card, message_id=msg.id if card.id not in existing_ids else None,
        )
    await repo.save_spec(session_id, final_spec.to_json(), final_spec.version)
    return c.ActionResponse(spec_version=final_spec.version, spec=final_spec.summary(), cards=changed, guide=guide)


def _supersede_for_new(new_cards: list[c.Card], existing: list[c.Card]) -> list[c.Card]:
    """Older ACTIVE cards of a re-issuable type that a freshly built card replaces
    (cards the action itself finishes are overlaid afterwards and win)."""
    out: list[c.Card] = []
    for card in new_cards:
        if card.id in {x.id for x in existing}:
            continue
        out.extend(K.superseded_by(card, existing))
    return out


async def _supersede_diff_cards(rec: SessionRecord, existing: list[c.Card], proposal_id: str) -> None:
    """A stale apply answers 409, so the dead diff card is persisted here."""
    for card in existing:
        if card.type == c.CardType.edit_diff and card.data.get("proposal_id") == proposal_id and card.status == c.CardStatus.active:
            dead = K.with_status(card, c.CardStatus.superseded, data_patch={"status": "stale", "stale": True})
            await repo.upsert_card(rec.id, org_id=rec.org_id, card=dead.model_copy(update={"actions": []}))


async def _confirm_edit(
    db: AsyncSession, actor: User, rec: SessionRecord, intent: K.ConfirmEditIntent, spec: Any,
    changed: list[c.Card], existing: list[c.Card], clicked_id: str,
) -> tuple[Any, c.Guide, list[c.Card]]:
    """The user clicked 'Run this change': re-check the protocol, create the
    proposal row (with the id the impact card carried), start its job."""
    protocol_id = _uuid_or_none(rec.protocol_id)
    if protocol_id is None:
        raise _err(c.ChatErrorCode.invalid_action, "This chat is not attached to a protocol.")
    protocol = await get_protocol_scoped(db, protocol_id, actor, load_drug=True)
    if protocol.status != ProtocolStatus.draft:
        raise _err(
            c.ChatErrorCode.invalid_action,
            f"This protocol is '{protocol.status.value}'. Chat edits only work on draft protocols; clone it to start a new draft.",
        )
    if (protocol.generation_status or "complete") != "complete":
        raise _err(c.ChatErrorCode.invalid_action, "This protocol is still generating (or failed); edits are not available.")
    if int(protocol.version) != int(intent.base_version):
        raise _err(
            c.ChatErrorCode.proposal_stale,
            "The protocol changed since this change was proposed. Describe the change again so it starts from the latest text.",
        )
    if intent.kind == "engine":
        try:
            edits.validate_engine_request(protocol, intent.request)
        except edits.ProposalError as exc:
            raise _err(exc.code, exc.message) from None
    try:
        await edits.create_proposal(
            db, proposal_id=intent.proposal_id, protocol=protocol, session_id=rec.id, actor_id=actor.id,
            kind=intent.kind, request=intent.request, base_version=intent.base_version,
        )
    except IntegrityError:
        await db.rollback()
        raise _err(c.ChatErrorCode.invalid_action, "That change request was already started.") from None
    await edits.start_job(intent.proposal_id)

    proposal = await edits.load_proposal(db, intent.proposal_id, org_id=actor.org_id, session_id=rec.id)
    assert proposal is not None
    await db.refresh(proposal)  # a job that ran inline has already moved it on
    info = {"id": intent.proposal_id, "kind": intent.kind, "status": proposal.status.value}
    after = K.after_edit_confirmed(spec, info, clicked_card_id=clicked_id, existing=existing)
    cards = _merge_cards(changed, after.cards)
    if proposal.error:  # a job that already failed inline: the card carries the reason
        cards = _merge_cards(cards, [
            K.build_edit_progress_card(
                after.spec.version, intent.proposal_id, intent.kind, proposal.status.value, proposal.error,
            ).model_copy(update={"id": x.id})
            for x in after.cards if x.type == c.CardType.edit_progress
        ])
    guide = after.guide
    if proposal.status.value == "ready":
        view = await edits.build_view(db, proposal, protocol)
        diff = _ready_view_card(after.spec.version, view)
        cards = _merge_cards(cards, _supersede_for_new([diff], existing), [diff])
        guide = c.Guide(text="The changes are ready. Review the differences, then apply or discard them.", chips=[])
    elif proposal.status.value == "failed":
        guide = c.Guide(text=proposal.error or "The edit could not be prepared.", chips=["Try the change again"])
    return after.spec, guide, cards


@router.post("/{sid}/actions", response_model=c.ActionResponse)
async def post_action(sid: uuid.UUID, payload: c.ActionRequest, request: Request, db: AsyncSession = Depends(get_db)):
    actor = await _authenticate_actor(request, db)
    row = await get_chat_session_scoped(db, sid, actor)
    rec = _record(row)
    _require_kind_role(actor, rec.kind)
    if rec.kind not in c.ACTION_SESSION_KINDS[payload.action]:
        raise _err(c.ChatErrorCode.invalid_action, f"'{payload.action.value}' is not available in a {rec.kind.value} chat.")
    if payload.action in c.ACTIONS_REQUIRING_LICENSE:
        await require_active_license(db, actor)

    # One writer at a time: refuse while a turn (or another action) holds the lock.
    lock_id = "act-" + uuid.uuid4().hex
    if not await repo.acquire_turn_lock(rec.id, lock_id):
        raise _err(c.ChatErrorCode.busy, "The assistant is working on this chat right now. Try again in a moment.")
    try:
        fresh = await repo.get_session(rec.id)  # the spec as it is now, under the lock
        assert fresh is not None
        return await _execute_action(db, request, actor, fresh, payload)
    finally:
        await repo.release_turn_lock(rec.id, lock_id)


# ---------------------------------------------------------------------------
# GET /api/chat/{sid}/proposals/{pid}
# ---------------------------------------------------------------------------


@router.get("/{sid}/proposals/{pid}", response_model=c.ProposalRead)
async def get_proposal(sid: uuid.UUID, pid: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    actor = await _authenticate_actor(request, db)
    row = await get_chat_session_scoped(db, sid, actor)
    rec = _record(row)
    proposal = await edits.load_proposal(db, pid, org_id=actor.org_id, session_id=rec.id)
    if proposal is None:
        raise _err(c.ChatErrorCode.not_found, "That proposed change was not found.")
    await edits.expire_if_stuck(db, proposal)
    protocol = (await db.execute(select(Protocol).where(Protocol.id == proposal.protocol_id))).scalar_one_or_none()
    await edits.refresh_staleness(db, proposal, protocol)
    view = await edits.build_view(db, proposal, protocol)
    # Polling this endpoint also makes the diff card exist for the transcript.
    await _synced_cards(db, rec)
    return edits.to_proposal_read(view)
