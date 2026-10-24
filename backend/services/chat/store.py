"""Chat persistence (plan B2): a repository interface, the SQLAlchemy
implementation over the wave-1 models, and an in-memory implementation for pure
tests.

Rules the implementations keep:

* APPEND-ONLY HISTORY. ``chat_messages`` rows are only ever inserted; their
  ``api_content`` (the exact Anthropic content blocks, thinking and tool_use
  included) is never rewritten. Only the UI projection ``display`` may be
  updated afterwards (to attach ``card_ids`` / ``flags``). Cards are mutable on
  purpose (status moves to superseded / done) without touching the history.
* ``database.AsyncSessionLocal()`` is looked up on the ``database`` module at CALL
  time and a fresh short-lived session is opened per operation, so a streaming
  turn never depends on the request's (already closed) session and the test
  fixture's patch of the factory applies.
* TRANSCRIPTS ARE PRIVATE TO THEIR CREATOR (user decision 2026-10-23): the
  viewer-scoped reads (:meth:`ChatRepository.get_session_for_viewer`,
  :meth:`ChatRepository.list_sessions`) require both the org and the creator to
  match. Protocols and budgets stay org-shared; chats do not.
* One AI turn per session at a time across workers, via ``turn_lock_id`` /
  ``turn_lock_at`` (a lock older than the stale window is taken over).

:func:`history_to_api_messages` turns stored rows into the ``messages`` array of
a model call, deterministically (the same rows always yield the same bytes, which
is what keeps prompt caching and preserved thinking valid).
"""
from __future__ import annotations

import copy
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Protocol, Sequence

from services.chat.contract import (
    Card,
    CardStatus,
    CardType,
    ChatErrorCode,
    ChatMessageDisplay,
    ChatSessionRead,
    MessageFlags,
    MessageRole,
    SessionKind,
    SessionStatus,
)

#: A turn lock older than this is considered abandoned (a crashed worker).
DEFAULT_TURN_LOCK_STALE_SECONDS = 600


def turn_lock_stale_seconds() -> int:
    try:
        return max(30, int(os.getenv("CHAT_TURN_LOCK_STALE_SECONDS", DEFAULT_TURN_LOCK_STALE_SECONDS)))
    except ValueError:
        return DEFAULT_TURN_LOCK_STALE_SECONDS


class ChatAccessError(Exception):
    """A session the viewer may not read: ``code`` is ``not_found`` or ``forbidden``."""

    def __init__(self, code: ChatErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class SessionRecord:
    id: str
    org_id: str
    created_by: str
    kind: SessionKind
    status: SessionStatus = SessionStatus.active
    title: str | None = None
    spec: dict[str, Any] = field(default_factory=dict)
    spec_version: int = 0
    protocol_id: str | None = None
    budget_id: str | None = None
    model: str | None = None
    turn_lock_id: str | None = None
    turn_lock_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MessageRecord:
    id: str
    session_id: str
    org_id: str
    seq: int
    role: MessageRole
    api_content: Any = None
    display: dict[str, Any] = field(default_factory=dict)
    created_by: str | None = None
    usage: dict[str, Any] | None = None
    model: str | None = None
    stop_reason: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def session_read(rec: SessionRecord) -> ChatSessionRead:
    return ChatSessionRead(
        id=rec.id, kind=rec.kind, status=rec.status, title=rec.title, spec_version=rec.spec_version,
        protocol_id=rec.protocol_id, budget_id=rec.budget_id, created_at=rec.created_at,
        updated_at=rec.updated_at, last_activity_at=rec.last_activity_at,
    )


def message_display(rec: MessageRecord) -> ChatMessageDisplay | None:
    """The transcript entry the browser renders, or None for rows that are never
    sent (tool results, the per-turn spec snapshot, empty assistant rows)."""
    if rec.role in (MessageRole.tool_results, MessageRole.system_note):
        return None
    d = rec.display or {}
    text = str(d.get("text") or "")
    card_ids = [str(x) for x in (d.get("card_ids") or [])]
    if rec.role == MessageRole.assistant and not text and not card_ids:
        return None
    return ChatMessageDisplay(
        id=rec.id, seq=rec.seq, role=rec.role, text=text, card_ids=card_ids,
        flags=MessageFlags(unverified_numbers=[str(x) for x in ((d.get("flags") or {}).get("unverified_numbers") or [])]),
        created_at=rec.created_at,
    )


def awaiting_reply(rows: Sequence[MessageRecord]) -> bool:
    """True when the last conversational row is a user message nobody answered
    (the stored ``initial_message``, or a turn that never completed)."""
    for rec in reversed(rows):
        if rec.role in (MessageRole.ui_action, MessageRole.system_note):
            continue
        return rec.role == MessageRole.user
    return False


# ---------------------------------------------------------------------------
# History -> API messages
# ---------------------------------------------------------------------------


def history_to_api_messages(
    rows: Sequence[MessageRecord], *, supports_system: bool = True,
) -> list[dict[str, Any]]:
    """Stored rows -> the ``messages`` array, deterministically.

    * user / assistant / tool_results rows replay their ``api_content`` verbatim
      (tool_results rows are ``role: "user"`` messages to the API).
    * rows without ``api_content`` (ui_action, an assistant row that produced no
      content) are skipped.
    * a ``system_note`` becomes a mid-conversation ``role: "system"`` message
      when it directly follows a user message and is last or followed by an
      assistant message (the API's placement rule); a note that would violate
      that (its turn never completed) is dropped. When the model does not
      support mid-conversation system messages the note is folded into the
      preceding user message as a trailing ``<system-reminder>`` text block.
    """
    msgs: list[dict[str, Any]] = []
    for rec in rows:
        if rec.role == MessageRole.user and rec.api_content:
            msgs.append({"role": "user", "content": copy.deepcopy(rec.api_content)})
        elif rec.role == MessageRole.tool_results and rec.api_content:
            msgs.append({"role": "user", "content": copy.deepcopy(rec.api_content)})
        elif rec.role == MessageRole.assistant and rec.api_content:
            msgs.append({"role": "assistant", "content": copy.deepcopy(rec.api_content)})
        elif rec.role == MessageRole.system_note and rec.api_content:
            text = rec.api_content if isinstance(rec.api_content, str) else str(rec.api_content)
            msgs.append({"role": "system", "content": text})

    out: list[dict[str, Any]] = []
    for i, m in enumerate(msgs):
        if m["role"] != "system":
            out.append(m)
            continue
        prev = out[-1] if out else None
        nxt = msgs[i + 1] if i + 1 < len(msgs) else None
        placement_ok = prev is not None and prev["role"] == "user" and (nxt is None or nxt["role"] == "assistant")
        if not placement_ok:
            continue
        if supports_system:
            out.append(m)
        else:
            content = prev["content"]
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
            blocks.append({"type": "text", "text": f"<system-reminder>\n{m['content']}\n</system-reminder>"})
            prev["content"] = blocks
    return out


# ---------------------------------------------------------------------------
# Repository interface
# ---------------------------------------------------------------------------


class ChatRepository(Protocol):
    """Everything the orchestrator and (next wave) the router persist."""

    async def create_session(
        self, *, org_id: str, created_by: str, kind: SessionKind, title: str | None = None,
        protocol_id: str | None = None, budget_id: str | None = None,
        spec: dict[str, Any] | None = None, spec_version: int = 0, model: str | None = None,
    ) -> SessionRecord: ...

    async def get_session(self, session_id: str) -> SessionRecord | None:
        """Unscoped read (the caller has already authorised)."""

    async def get_session_for_viewer(self, session_id: str, *, org_id: str, viewer_id: str) -> SessionRecord:
        """Creator-only read: raises :class:`ChatAccessError` (``not_found`` when
        missing, ``forbidden`` for another org or another user)."""

    async def list_sessions(
        self, *, org_id: str, created_by: str, kind: SessionKind | None = None, limit: int = 20,
    ) -> list[SessionRecord]:
        """The viewer's own sessions, most recently active first."""

    async def update_session(self, session_id: str, **fields: Any) -> None:
        """Update ``title`` / ``status`` / ``protocol_id`` / ``budget_id`` / ``model``."""

    async def save_spec(self, session_id: str, spec: dict[str, Any], spec_version: int) -> None: ...

    async def list_messages(self, session_id: str) -> list[MessageRecord]:
        """All rows, ordered by ``seq``."""

    async def append_message(
        self, session_id: str, *, org_id: str, role: MessageRole, api_content: Any, display: dict[str, Any],
        created_by: str | None = None, usage: dict[str, Any] | None = None, model: str | None = None,
        stop_reason: str | None = None,
    ) -> MessageRecord:
        """Insert the next row (``seq`` = previous + 1). Never edits an existing row."""

    async def set_message_display(self, message_id: str, display: dict[str, Any]) -> None:
        """Replace the UI projection of one row (``api_content`` is never touched)."""

    async def find_user_message_by_client_turn(self, session_id: str, client_turn_id: str) -> MessageRecord | None: ...

    async def upsert_card(self, session_id: str, *, org_id: str, card: Card, message_id: str | None = None) -> None: ...

    async def get_card(self, session_id: str, card_id: str) -> Card | None: ...

    async def list_cards(self, session_id: str) -> list[Card]:
        """All cards of the session, oldest first."""

    async def supersede_cards(self, session_id: str, card_type: CardType, *, except_card_id: str) -> list[Card]:
        """Mark the session's ACTIVE cards of ``card_type`` superseded; returns the changed cards."""

    async def acquire_turn_lock(self, session_id: str, turn_id: str, *, stale_after_seconds: int | None = None) -> bool: ...

    async def touch_turn_lock(self, session_id: str, turn_id: str) -> None: ...

    async def release_turn_lock(self, session_id: str, turn_id: str) -> None: ...

    async def is_turn_locked(self, session_id: str, *, stale_after_seconds: int | None = None) -> bool: ...

    async def count_user_messages(self, session_id: str) -> int:
        """User rows in the session (the per-session turn cap)."""

    async def count_user_messages_since(self, user_id: str, since: datetime) -> int:
        """User rows written by ``user_id`` since ``since`` (the per-day turn cap)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _card_from_row(row_type: str, row_id: Any, status: Any, data: dict[str, Any], spec_version: int, extra: dict[str, Any]) -> Card:
    return Card(
        id=str(row_id), type=CardType(row_type), status=CardStatus(getattr(status, "value", status)),
        spec_version=spec_version, title=str(extra.get("title") or ""), data=data,
        actions=extra.get("actions") or [],
    )


# ---------------------------------------------------------------------------
# In-memory implementation (pure tests; also handy for a no-database MOCK run)
# ---------------------------------------------------------------------------


class InMemoryRepository:
    """A dict-backed :class:`ChatRepository` with the same semantics as the SQL
    one (append-only seq, creator-only reads, stale-aware turn lock). ``clock``
    is injectable so tests can age a lock."""

    def __init__(self, clock: Callable[[], datetime] = _utcnow):
        self.clock = clock
        self.sessions: dict[str, SessionRecord] = {}
        self.messages: dict[str, list[MessageRecord]] = {}
        self.cards: dict[str, dict[str, Card]] = {}
        self.card_order: dict[str, list[str]] = {}
        self.card_message: dict[str, dict[str, str | None]] = {}
        #: Every ``save_spec`` call, in order (tests assert on persistence).
        self.spec_saves: list[tuple[str, int]] = []

    # -- sessions
    async def create_session(
        self, *, org_id: str, created_by: str, kind: SessionKind, title: str | None = None,
        protocol_id: str | None = None, budget_id: str | None = None,
        spec: dict[str, Any] | None = None, spec_version: int = 0, model: str | None = None,
    ) -> SessionRecord:
        now = self.clock()
        rec = SessionRecord(
            id=str(uuid.uuid4()), org_id=org_id, created_by=str(created_by), kind=SessionKind(kind), title=title,
            spec=copy.deepcopy(spec or {}), spec_version=spec_version, protocol_id=protocol_id, budget_id=budget_id,
            model=model, created_at=now, updated_at=now, last_activity_at=now,
        )
        self.sessions[rec.id] = rec
        self.messages[rec.id] = []
        self.cards[rec.id] = {}
        self.card_order[rec.id] = []
        self.card_message[rec.id] = {}
        return copy.deepcopy(rec)

    async def get_session(self, session_id: str) -> SessionRecord | None:
        rec = self.sessions.get(session_id)
        return copy.deepcopy(rec) if rec else None

    async def get_session_for_viewer(self, session_id: str, *, org_id: str, viewer_id: str) -> SessionRecord:
        rec = self.sessions.get(session_id)
        if rec is None:
            raise ChatAccessError(ChatErrorCode.not_found, "Chat not found.")
        if rec.org_id != org_id:
            raise ChatAccessError(ChatErrorCode.forbidden, "Chat belongs to a different organization.")
        if rec.created_by != str(viewer_id):
            raise ChatAccessError(ChatErrorCode.forbidden, "Chat transcripts are visible only to the person who started them.")
        return copy.deepcopy(rec)

    async def list_sessions(
        self, *, org_id: str, created_by: str, kind: SessionKind | None = None, limit: int = 20,
    ) -> list[SessionRecord]:
        rows = [
            r for r in self.sessions.values()
            if r.org_id == org_id and r.created_by == str(created_by) and (kind is None or r.kind == kind)
        ]
        rows.sort(key=lambda r: r.last_activity_at, reverse=True)
        return [copy.deepcopy(r) for r in rows[:limit]]

    async def update_session(self, session_id: str, **fields: Any) -> None:
        rec = self.sessions[session_id]
        for k, v in fields.items():
            if k not in ("title", "status", "protocol_id", "budget_id", "model"):
                raise ValueError(f"cannot update {k!r}")
            setattr(rec, k, v)
        rec.updated_at = self.clock()

    async def save_spec(self, session_id: str, spec: dict[str, Any], spec_version: int) -> None:
        rec = self.sessions[session_id]
        rec.spec = copy.deepcopy(spec)
        rec.spec_version = spec_version
        rec.updated_at = rec.last_activity_at = self.clock()
        self.spec_saves.append((session_id, spec_version))

    # -- messages
    async def list_messages(self, session_id: str) -> list[MessageRecord]:
        return [copy.deepcopy(m) for m in sorted(self.messages.get(session_id, []), key=lambda m: m.seq)]

    async def append_message(
        self, session_id: str, *, org_id: str, role: MessageRole, api_content: Any, display: dict[str, Any],
        created_by: str | None = None, usage: dict[str, Any] | None = None, model: str | None = None,
        stop_reason: str | None = None,
    ) -> MessageRecord:
        rows = self.messages[session_id]
        rec = MessageRecord(
            id=str(uuid.uuid4()), session_id=session_id, org_id=org_id, seq=(rows[-1].seq + 1) if rows else 1,
            role=MessageRole(role), api_content=copy.deepcopy(api_content), display=copy.deepcopy(display),
            created_by=str(created_by) if created_by else None, usage=copy.deepcopy(usage), model=model,
            stop_reason=stop_reason, created_at=self.clock(),
        )
        rows.append(rec)
        self.sessions[session_id].last_activity_at = rec.created_at
        return copy.deepcopy(rec)

    async def set_message_display(self, message_id: str, display: dict[str, Any]) -> None:
        for rows in self.messages.values():
            for rec in rows:
                if rec.id == message_id:
                    rec.display = copy.deepcopy(display)
                    return
        raise KeyError(message_id)

    async def find_user_message_by_client_turn(self, session_id: str, client_turn_id: str) -> MessageRecord | None:
        for rec in self.messages.get(session_id, []):
            if rec.role == MessageRole.user and (rec.display or {}).get("client_turn_id") == client_turn_id:
                return copy.deepcopy(rec)
        return None

    # -- cards
    async def upsert_card(self, session_id: str, *, org_id: str, card: Card, message_id: str | None = None) -> None:
        if card.id not in self.cards[session_id]:
            self.card_order[session_id].append(card.id)
            self.card_message[session_id][card.id] = message_id
        elif message_id is not None:
            self.card_message[session_id][card.id] = message_id
        self.cards[session_id][card.id] = card.model_copy(deep=True)

    async def get_card(self, session_id: str, card_id: str) -> Card | None:
        card = self.cards.get(session_id, {}).get(card_id)
        return card.model_copy(deep=True) if card else None

    async def list_cards(self, session_id: str) -> list[Card]:
        return [self.cards[session_id][i].model_copy(deep=True) for i in self.card_order.get(session_id, [])]

    async def supersede_cards(self, session_id: str, card_type: CardType, *, except_card_id: str) -> list[Card]:
        changed: list[Card] = []
        for cid in self.card_order.get(session_id, []):
            card = self.cards[session_id][cid]
            if card.type == card_type and card.status == CardStatus.active and card.id != except_card_id:
                card = card.model_copy(update={"status": CardStatus.superseded})
                self.cards[session_id][cid] = card
                changed.append(card.model_copy(deep=True))
        return changed

    # -- lock
    def _stale_cutoff(self, stale_after_seconds: int | None) -> datetime:
        return self.clock() - timedelta(seconds=stale_after_seconds or turn_lock_stale_seconds())

    async def acquire_turn_lock(self, session_id: str, turn_id: str, *, stale_after_seconds: int | None = None) -> bool:
        rec = self.sessions[session_id]
        if rec.turn_lock_id and rec.turn_lock_at and rec.turn_lock_at > self._stale_cutoff(stale_after_seconds):
            return False
        rec.turn_lock_id, rec.turn_lock_at = turn_id, self.clock()
        return True

    async def touch_turn_lock(self, session_id: str, turn_id: str) -> None:
        rec = self.sessions[session_id]
        if rec.turn_lock_id == turn_id:
            rec.turn_lock_at = self.clock()

    async def release_turn_lock(self, session_id: str, turn_id: str) -> None:
        rec = self.sessions[session_id]
        if rec.turn_lock_id == turn_id:
            rec.turn_lock_id = rec.turn_lock_at = None

    async def is_turn_locked(self, session_id: str, *, stale_after_seconds: int | None = None) -> bool:
        rec = self.sessions[session_id]
        return bool(rec.turn_lock_id and rec.turn_lock_at and rec.turn_lock_at > self._stale_cutoff(stale_after_seconds))

    # -- counts
    async def count_user_messages(self, session_id: str) -> int:
        return sum(1 for m in self.messages.get(session_id, []) if m.role == MessageRole.user)

    async def count_user_messages_since(self, user_id: str, since: datetime) -> int:
        return sum(
            1 for rows in self.messages.values() for m in rows
            if m.role == MessageRole.user and m.created_by == str(user_id) and m.created_at >= since
        )


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


def _uid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class SqlChatRepository:
    """:class:`ChatRepository` over ``chat_sessions`` / ``chat_messages`` /
    ``chat_cards``. Each call opens its own session from
    ``database.AsyncSessionLocal()`` (looked up at call time) and commits before
    returning; nothing is shared with the HTTP request's session."""

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        import database

        async with database.AsyncSessionLocal() as session:
            yield session

    # -- conversions
    @staticmethod
    def _session_record(row: Any) -> SessionRecord:
        return SessionRecord(
            id=str(row.id), org_id=row.org_id, created_by=str(row.created_by),
            kind=SessionKind(getattr(row.kind, "value", row.kind)),
            status=SessionStatus(getattr(row.status, "value", row.status)),
            title=row.title, spec=dict(row.spec or {}), spec_version=int(row.spec_version or 0),
            protocol_id=str(row.protocol_id) if row.protocol_id else None,
            budget_id=str(row.budget_id) if row.budget_id else None, model=row.model,
            turn_lock_id=row.turn_lock_id, turn_lock_at=row.turn_lock_at,
            created_at=row.created_at, updated_at=row.updated_at, last_activity_at=row.last_activity_at,
        )

    @staticmethod
    def _message_record(row: Any) -> MessageRecord:
        return MessageRecord(
            id=str(row.id), session_id=str(row.session_id), org_id=row.org_id, seq=int(row.seq),
            role=MessageRole(getattr(row.role, "value", row.role)), api_content=row.api_content,
            display=dict(row.display or {}), created_by=str(row.created_by) if row.created_by else None,
            usage=row.usage, model=row.model, stop_reason=row.stop_reason, created_at=row.created_at,
        )

    @staticmethod
    def _card(row: Any) -> Card:
        data = dict(row.data or {})
        meta = data.pop("__card__", {}) if isinstance(data.get("__card__"), dict) else {}
        return _card_from_row(row.type, row.id, row.status, data, int(row.spec_version or 0), meta)

    # -- sessions
    async def create_session(
        self, *, org_id: str, created_by: str, kind: SessionKind, title: str | None = None,
        protocol_id: str | None = None, budget_id: str | None = None,
        spec: dict[str, Any] | None = None, spec_version: int = 0, model: str | None = None,
    ) -> SessionRecord:
        from models import ChatSession

        async with self._session() as s:
            row = ChatSession(
                id=uuid.uuid4(), org_id=org_id, created_by=_uid(created_by), kind=SessionKind(kind).value,
                title=title, spec=spec or {}, spec_version=spec_version,
                protocol_id=_uid(protocol_id) if protocol_id else None,
                budget_id=_uid(budget_id) if budget_id else None, model=model,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return self._session_record(row)

    async def get_session(self, session_id: str) -> SessionRecord | None:
        from models import ChatSession

        try:
            sid = _uid(session_id)
        except ValueError:
            return None
        async with self._session() as s:
            row = await s.get(ChatSession, sid)
            return self._session_record(row) if row else None

    async def get_session_for_viewer(self, session_id: str, *, org_id: str, viewer_id: str) -> SessionRecord:
        rec = await self.get_session(session_id)
        if rec is None:
            raise ChatAccessError(ChatErrorCode.not_found, "Chat not found.")
        if rec.org_id != org_id:
            raise ChatAccessError(ChatErrorCode.forbidden, "Chat belongs to a different organization.")
        if rec.created_by != str(viewer_id):
            raise ChatAccessError(ChatErrorCode.forbidden, "Chat transcripts are visible only to the person who started them.")
        return rec

    async def list_sessions(
        self, *, org_id: str, created_by: str, kind: SessionKind | None = None, limit: int = 20,
    ) -> list[SessionRecord]:
        from sqlalchemy import select

        from models import ChatSession

        async with self._session() as s:
            stmt = select(ChatSession).where(ChatSession.org_id == org_id, ChatSession.created_by == _uid(created_by))
            if kind is not None:
                stmt = stmt.where(ChatSession.kind == SessionKind(kind).value)
            stmt = stmt.order_by(ChatSession.last_activity_at.desc()).limit(max(1, min(int(limit), 100)))
            return [self._session_record(r) for r in (await s.execute(stmt)).scalars().all()]

    async def update_session(self, session_id: str, **fields: Any) -> None:
        from sqlalchemy import func, update

        from models import ChatSession

        allowed = {"title", "status", "protocol_id", "budget_id", "model"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {sorted(bad)}")
        values: dict[str, Any] = {}
        for k, v in fields.items():
            if k in ("protocol_id", "budget_id"):
                v = _uid(v) if v else None
            elif k == "status":
                v = SessionStatus(v).value
            values[k] = v
        if not values:
            return
        async with self._session() as s:
            await s.execute(update(ChatSession).where(ChatSession.id == _uid(session_id)).values(**values, updated_at=func.now()))
            await s.commit()

    async def save_spec(self, session_id: str, spec: dict[str, Any], spec_version: int) -> None:
        from sqlalchemy import func, update

        from models import ChatSession

        async with self._session() as s:
            await s.execute(
                update(ChatSession).where(ChatSession.id == _uid(session_id))
                .values(spec=spec, spec_version=spec_version, updated_at=func.now(), last_activity_at=func.now())
            )
            await s.commit()

    # -- messages
    async def list_messages(self, session_id: str) -> list[MessageRecord]:
        from sqlalchemy import select

        from models import ChatMessage

        async with self._session() as s:
            stmt = select(ChatMessage).where(ChatMessage.session_id == _uid(session_id)).order_by(ChatMessage.seq)
            return [self._message_record(r) for r in (await s.execute(stmt)).scalars().all()]

    async def append_message(
        self, session_id: str, *, org_id: str, role: MessageRole, api_content: Any, display: dict[str, Any],
        created_by: str | None = None, usage: dict[str, Any] | None = None, model: str | None = None,
        stop_reason: str | None = None,
    ) -> MessageRecord:
        from sqlalchemy import func, select, update
        from sqlalchemy.exc import IntegrityError

        from models import ChatMessage, ChatSession

        sid = _uid(session_id)
        last_error: Exception | None = None
        for _attempt in range(5):
            async with self._session() as s:
                try:
                    nxt = (await s.execute(
                        select(func.coalesce(func.max(ChatMessage.seq), 0) + 1).where(ChatMessage.session_id == sid)
                    )).scalar_one()
                    row = ChatMessage(
                        id=uuid.uuid4(), session_id=sid, org_id=org_id, seq=int(nxt), role=MessageRole(role).value,
                        api_content=api_content, display=display or {},
                        created_by=_uid(created_by) if created_by else None, usage=usage, model=model,
                        stop_reason=stop_reason,
                    )
                    s.add(row)
                    await s.execute(update(ChatSession).where(ChatSession.id == sid).values(last_activity_at=func.now()))
                    await s.commit()
                    await s.refresh(row)
                    return self._message_record(row)
                except IntegrityError as exc:  # a concurrent append took this seq: retry
                    await s.rollback()
                    last_error = exc
        raise RuntimeError("could not append a chat message after retries") from last_error

    async def set_message_display(self, message_id: str, display: dict[str, Any]) -> None:
        from sqlalchemy import update

        from models import ChatMessage

        async with self._session() as s:
            await s.execute(update(ChatMessage).where(ChatMessage.id == _uid(message_id)).values(display=display))
            await s.commit()

    async def find_user_message_by_client_turn(self, session_id: str, client_turn_id: str) -> MessageRecord | None:
        from sqlalchemy import select

        from models import ChatMessage

        async with self._session() as s:
            stmt = (
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == _uid(session_id),
                    ChatMessage.role == MessageRole.user.value,
                    ChatMessage.display["client_turn_id"].astext == client_turn_id,
                )
                .limit(1)
            )
            row = (await s.execute(stmt)).scalars().first()
            return self._message_record(row) if row else None

    # -- cards
    @staticmethod
    def _card_data(card: Card) -> dict[str, Any]:
        """``chat_cards`` has no title / actions columns: they travel inside
        ``data`` under a reserved key and are split back out on read."""
        data = dict(card.data)
        data["__card__"] = {"title": card.title, "actions": [a.model_dump(mode="json") for a in card.actions]}
        return data

    async def upsert_card(self, session_id: str, *, org_id: str, card: Card, message_id: str | None = None) -> None:
        from sqlalchemy import func

        from models import ChatCard

        async with self._session() as s:
            row = await s.get(ChatCard, _uid(card.id))
            if row is None:
                s.add(ChatCard(
                    id=_uid(card.id), session_id=_uid(session_id), org_id=org_id,
                    message_id=_uid(message_id) if message_id else None, type=card.type.value,
                    status=card.status.value, data=self._card_data(card), spec_version=card.spec_version,
                ))
            else:
                row.status = card.status.value
                row.data = self._card_data(card)
                row.spec_version = card.spec_version
                if message_id:
                    row.message_id = _uid(message_id)
                row.updated_at = func.now()
            await s.commit()

    async def get_card(self, session_id: str, card_id: str) -> Card | None:
        from models import ChatCard

        async with self._session() as s:
            row = await s.get(ChatCard, _uid(card_id))
            if row is None or str(row.session_id) != str(session_id):
                return None
            return self._card(row)

    async def list_cards(self, session_id: str) -> list[Card]:
        from sqlalchemy import select

        from models import ChatCard

        async with self._session() as s:
            stmt = select(ChatCard).where(ChatCard.session_id == _uid(session_id)).order_by(ChatCard.created_at)
            return [self._card(r) for r in (await s.execute(stmt)).scalars().all()]

    async def supersede_cards(self, session_id: str, card_type: CardType, *, except_card_id: str) -> list[Card]:
        from sqlalchemy import func, select

        from models import ChatCard

        async with self._session() as s:
            stmt = select(ChatCard).where(
                ChatCard.session_id == _uid(session_id), ChatCard.type == card_type.value,
                ChatCard.status == CardStatus.active.value, ChatCard.id != _uid(except_card_id),
            )
            rows = (await s.execute(stmt)).scalars().all()
            for r in rows:
                r.status = CardStatus.superseded.value
                r.updated_at = func.now()
            await s.commit()
            return [self._card(r) for r in rows]

    # -- lock
    async def acquire_turn_lock(self, session_id: str, turn_id: str, *, stale_after_seconds: int | None = None) -> bool:
        from sqlalchemy import or_, update

        from models import ChatSession

        now = _utcnow()
        cutoff = now - timedelta(seconds=stale_after_seconds or turn_lock_stale_seconds())
        async with self._session() as s:
            res = await s.execute(
                update(ChatSession)
                .where(
                    ChatSession.id == _uid(session_id),
                    or_(ChatSession.turn_lock_id.is_(None), ChatSession.turn_lock_at.is_(None), ChatSession.turn_lock_at < cutoff),
                )
                .values(turn_lock_id=turn_id, turn_lock_at=now)
            )
            await s.commit()
            return (res.rowcount or 0) == 1

    async def touch_turn_lock(self, session_id: str, turn_id: str) -> None:
        from sqlalchemy import update

        from models import ChatSession

        async with self._session() as s:
            await s.execute(
                update(ChatSession).where(ChatSession.id == _uid(session_id), ChatSession.turn_lock_id == turn_id)
                .values(turn_lock_at=_utcnow())
            )
            await s.commit()

    async def release_turn_lock(self, session_id: str, turn_id: str) -> None:
        from sqlalchemy import update

        from models import ChatSession

        async with self._session() as s:
            await s.execute(
                update(ChatSession).where(ChatSession.id == _uid(session_id), ChatSession.turn_lock_id == turn_id)
                .values(turn_lock_id=None, turn_lock_at=None)
            )
            await s.commit()

    async def is_turn_locked(self, session_id: str, *, stale_after_seconds: int | None = None) -> bool:
        rec = await self.get_session(session_id)
        if rec is None or not rec.turn_lock_id or rec.turn_lock_at is None:
            return False
        at = rec.turn_lock_at if rec.turn_lock_at.tzinfo else rec.turn_lock_at.replace(tzinfo=timezone.utc)
        return at > _utcnow() - timedelta(seconds=stale_after_seconds or turn_lock_stale_seconds())

    # -- counts
    async def count_user_messages(self, session_id: str) -> int:
        from sqlalchemy import func, select

        from models import ChatMessage

        async with self._session() as s:
            return int((await s.execute(
                select(func.count()).select_from(ChatMessage).where(
                    ChatMessage.session_id == _uid(session_id), ChatMessage.role == MessageRole.user.value)
            )).scalar_one())

    async def count_user_messages_since(self, user_id: str, since: datetime) -> int:
        from sqlalchemy import func, select

        from models import ChatMessage

        async with self._session() as s:
            return int((await s.execute(
                select(func.count()).select_from(ChatMessage).where(
                    ChatMessage.created_by == _uid(user_id), ChatMessage.role == MessageRole.user.value,
                    ChatMessage.created_at >= since)
            )).scalar_one())
