"""Shared wire contract for the chat feature (docs/redesign-chat-plan.md, B1-B9).

Everything that crosses the browser <-> server boundary for chat is defined
here: enums, the card shape, action requests/responses, the draft-spec summary,
request/response models for every route, and the SSE event payloads. The
TypeScript mirror is ``frontend/src/chat/types.ts`` -- keep the two in lockstep.

This module is deliberately PURE: it imports only pydantic and the standard
library (no database, no FastAPI, no Anthropic SDK, no other ``services``
modules), so any agent can import it from a pure test without a test database.
Later waves may ADD to it; do not rename or remove anything.

ROUTES (all under ``/api``; NO trailing slash -- a trailing slash on a route
registered without one triggers a cross-origin 307 that drops Authorization):

    POST   /chat                         CreateChatRequest   -> ChatSessionRead
    GET    /chat?kind=&limit=                                -> list[ChatSessionListItem]
    GET    /chat/stream-check            (SSE, no AI)        -> event "tick" x STREAM_CHECK_EVENTS
    GET    /chat/{sid}                                       -> ChatSessionDetail
    PATCH  /chat/{sid}                   PatchChatRequest    -> ChatSessionRead
    POST   /chat/{sid}/turns             TurnRequest         -> text/event-stream (SseEventName)
    POST   /chat/{sid}/actions           ActionRequest       -> ActionResponse
    GET    /chat/{sid}/proposals/{pid}                       -> ProposalRead

``/chat/stream-check`` must be declared BEFORE ``/chat/{sid}`` in the router.

HTTP errors on chat routes carry ``detail`` as either a plain string (the
existing 402 license / 429 rate-limit wording) or ``{"code", "message"}`` built
with :func:`error_detail`. Where a status is shared by several conditions
(409 busy / stale_spec / proposal_stale, 429 rate_limited / daily_cap /
session_cap) the ``code`` is what distinguishes them; see
:data:`ERROR_HTTP_STATUS`. Failures INSIDE an open stream are an ``error`` SSE
event instead (HTTP status is already 200 by then).
"""

import json
import re
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic_core import to_jsonable_python

# ---------------------------------------------------------------------------
# Limits and defaults (constants; runtime-tunable ones are read from env by the
# code that enforces them -- these are the documented defaults)
# ---------------------------------------------------------------------------

MAX_TURN_TEXT = 4000            # TurnRequest.text, characters
MAX_INITIAL_MESSAGE = 4000      # CreateChatRequest.initial_message, characters
MAX_TITLE = 200                 # chat_sessions.title (string 200)
MAX_CLIENT_TURN_ID = 64         # TurnRequest.client_turn_id
MAX_ID_LENGTH = 64              # generic id strings (card / action / field ids)
MAX_LABEL_LENGTH = 200          # card action labels, spec field labels
MAX_INN_LENGTH = 64             # drug INN (matches schemas.DrugCreate)
MAX_INDICATION_LENGTH = 200     # set_indication text (matches schemas user_indication)
MAX_EXCLUSION_TEXT = 300        # toggle_exclusion text
MAX_DESIGN_KEY_LENGTH = 64      # set_design_decision key
MAX_DESIGN_VALUE_LENGTH = 200   # set_design_decision value
MAX_GUIDELINE_VERSION_LENGTH = 64

DEFAULT_LIST_LIMIT = 20         # GET /chat default `limit`
MAX_LIST_LIMIT = 100            # GET /chat max `limit`

CHAT_DAILY_TURN_CAP = 200       # turns per user per day (llm_usage ledger)
CHAT_SESSION_TURN_CAP = 80      # turns per session
CHAT_MAX_TOOL_ITERATIONS = 8    # model calls per turn
CHAT_MAX_TOKENS_PER_CALL = 4096
PROPOSAL_STUCK_MINUTES = 30     # a proposal "generating" longer than this reads as failed

SSE_HEARTBEAT_SECONDS = 15      # `: ping` comment cadence on every stream
STREAM_CHECK_EVENTS = 5         # GET /chat/stream-check: 5 ticks ...
STREAM_CHECK_INTERVAL_SECONDS = 4.0  # ... ~4 s apart (about 20 s total)

DEFAULT_GUIDELINE_VERSION = "FDA-BE-2014"

#: The pure-comment heartbeat line (ignored by SSE parsers).
SSE_PING = ": ping\n\n"

# Drug INN: a strict SUBSET of schemas.DrugCreate's pattern (letters, spaces,
# hyphens only; the server's `\s` also admits tabs/newlines). Stricter here is
# safe -- anything this accepts, DrugCreate accepts too.
_INN_RE = re.compile(r"^[A-Za-z][A-Za-z \-]{1,63}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_CLIENT_TURN_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")


# ---------------------------------------------------------------------------
# Reusable annotated types
# ---------------------------------------------------------------------------


def _id_to_str(value: Any) -> Any:
    """Accept ``uuid.UUID`` (straight off an ORM row) and emit a plain ``str``."""
    return str(value) if isinstance(value, uuid.UUID) else value


#: Server-produced id (UUID string). Accepts a ``uuid.UUID`` so ORM rows can be
#: validated directly; always serialises as a string.
IdStr = Annotated[str, BeforeValidator(_id_to_str), Field(min_length=1, max_length=MAX_ID_LENGTH)]


def _check_uuid(value: str) -> str:
    if not _UUID_RE.match(value):
        raise ValueError("must be a UUID in canonical 8-4-4-4-12 form")
    return value.lower()


#: Client-supplied id that the server will use to look something up. Canonical
#: UUID text only (so a malformed id is a 422, never a 500 from the database).
UuidStr = Annotated[str, AfterValidator(_check_uuid)]


def _check_inn(value: str) -> str:
    value = value.strip()
    if not _INN_RE.match(value):
        raise ValueError("inn must be 2-64 letters, spaces, or hyphens only")
    return value.lower()


InnStr = Annotated[str, AfterValidator(_check_inn)]


def _check_client_turn_id(value: str) -> str:
    if not _CLIENT_TURN_ID_RE.match(value):
        raise ValueError("client_turn_id must be 1-64 characters of letters, digits, '.', '_', ':' or '-'")
    return value


ClientTurnId = Annotated[str, AfterValidator(_check_client_turn_id)]


def _clean_optional_text(value: Any) -> Any:
    """Strip surrounding whitespace; blank becomes None."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _optional_text(max_length: int) -> Any:
    return Annotated[
        Annotated[str, StringConstraints(max_length=max_length)] | None,
        BeforeValidator(_clean_optional_text),
    ]


#: Optional free text: surrounding whitespace stripped, blank -> None, then the
#: character limit is enforced on what remains.
TurnText = _optional_text(MAX_TURN_TEXT)
InitialMessageText = _optional_text(MAX_INITIAL_MESSAGE)
TitleText = _optional_text(MAX_TITLE)
IndicationText = _optional_text(MAX_INDICATION_LENGTH)


def _ensure_utc(value: datetime) -> datetime:
    """Naive datetimes are treated as UTC; aware ones are converted to UTC.
    Serialises as ``...Z`` so the browser never has to guess the zone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]


def _plain_json_object(value: dict[str, Any]) -> dict[str, Any]:
    """Coerce to plain JSON primitives (UUID/datetime/Enum -> str, tuple -> list)
    and reject anything JSONB / JSON.parse cannot round-trip (NaN, +-Infinity,
    arbitrary objects). A card ``data`` dict is therefore always safe to store
    and to send."""
    plain = to_jsonable_python(value)
    json.dumps(plain, allow_nan=False)  # ValueError on NaN / Infinity
    return plain


#: A dict guaranteed to be plain JSON after validation.
JsonObject = Annotated[dict[str, Any], AfterValidator(_plain_json_object)]


class _Model(BaseModel):
    """Base for server -> client models: unknown keys are ignored so a payload
    written by a newer/older build never breaks a reader."""

    model_config = ConfigDict(extra="ignore")


class _Request(BaseModel):
    """Base for client -> server models: unknown keys are a 422 (catches typos
    and stale clients early)."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Enums (string values are the wire format AND the DB values, B2)
# ---------------------------------------------------------------------------


class SessionKind(StrEnum):
    protocol = "protocol"
    budget = "budget"
    protocol_edit = "protocol_edit"


class SessionStatus(StrEnum):
    active = "active"
    completed = "completed"
    archived = "archived"


class MessageRole(StrEnum):
    user = "user"
    assistant = "assistant"
    tool_results = "tool_results"
    ui_action = "ui_action"
    system_note = "system_note"


class CardType(StrEnum):
    # protocol set-up
    drug_matches = "drug_matches"
    drug_add_offer = "drug_add_offer"
    study_type = "study_type"
    population = "population"
    cv_override_warning = "cv_override_warning"
    interaction_screening = "interaction_screening"
    design_decisions = "design_decisions"
    indication = "indication"
    guideline = "guideline"
    # protocol generation
    protocol_summary = "protocol_summary"
    generation_progress = "generation_progress"
    protocol_ready = "protocol_ready"
    # budget
    budget_preview = "budget_preview"
    budget_summary = "budget_summary"
    budget_ready = "budget_ready"
    # protocol edit
    protocol_overview = "protocol_overview"
    edit_impact = "edit_impact"
    edit_progress = "edit_progress"
    edit_diff = "edit_diff"
    # generic
    notice = "notice"


class CardStatus(StrEnum):
    active = "active"
    superseded = "superseded"
    done = "done"
    error = "error"


class ActionType(StrEnum):
    # protocol
    select_drug = "select_drug"
    add_drug = "add_drug"
    set_study_type = "set_study_type"
    set_guideline = "set_guideline"
    update_population = "update_population"
    toggle_exclusion = "toggle_exclusion"
    set_design_decision = "set_design_decision"
    set_indication = "set_indication"
    confirm_design = "confirm_design"
    generate_protocol = "generate_protocol"
    # budget
    set_budget_fields = "set_budget_fields"
    link_protocol = "link_protocol"
    create_budget = "create_budget"
    # protocol edit
    confirm_edit = "confirm_edit"
    apply_edit = "apply_edit"
    discard_edit = "discard_edit"


class FieldStatus(StrEnum):
    empty = "empty"
    default = "default"
    engine = "engine"
    proposed = "proposed"
    confirmed = "confirmed"


class FieldSource(StrEnum):
    default = "default"
    engine = "engine"
    user_click = "user_click"
    user_text = "user_text"


class ProposalKind(StrEnum):
    prose = "prose"
    engine = "engine"


class ProposalStatus(StrEnum):
    proposed = "proposed"
    generating = "generating"
    ready = "ready"
    applied = "applied"
    discarded = "discarded"
    failed = "failed"
    stale = "stale"


class ChatErrorCode(StrEnum):
    """Machine-readable error codes (HTTP ``detail.code`` and SSE ``error.code``)."""

    busy = "busy"                    # 409: another turn holds the session lock
    stale_spec = "stale_spec"        # 409: action's spec_version is behind the server's
    proposal_stale = "proposal_stale"  # 409: protocol changed since the proposal was made
    license_required = "license_required"  # 402
    rate_limited = "rate_limited"    # 429 (per-minute limiter)
    daily_cap = "daily_cap"          # 429 (turns per user per day)
    session_cap = "session_cap"      # 429 (turns per session)
    forbidden = "forbidden"          # 403
    not_found = "not_found"          # 404
    validation = "validation"        # 422
    invalid_action = "invalid_action"  # 422 (action not valid for this card/session kind/state)
    model_error = "model_error"      # SSE: the LLM call failed
    internal = "internal"            # SSE / 500
    stream_interrupted = "stream_interrupted"  # client-synthesised: stream closed before turn_end


#: The HTTP status each code is sent with on a normal (non-stream) response.
ERROR_HTTP_STATUS: dict[ChatErrorCode, int] = {
    ChatErrorCode.busy: 409,
    ChatErrorCode.stale_spec: 409,
    ChatErrorCode.proposal_stale: 409,
    ChatErrorCode.license_required: 402,
    ChatErrorCode.rate_limited: 429,
    ChatErrorCode.daily_cap: 429,
    ChatErrorCode.session_cap: 429,
    ChatErrorCode.forbidden: 403,
    ChatErrorCode.not_found: 404,
    ChatErrorCode.validation: 422,
    ChatErrorCode.invalid_action: 422,
    ChatErrorCode.model_error: 502,
    ChatErrorCode.internal: 500,
}

#: Study types, as validated everywhere else in the app.
StudyTypeLiteral = Literal["BE", "biosimilar_pk", "food_effect"]
CardActionVariant = Literal["primary", "secondary", "ghost", "danger"]
NoticeLevel = Literal["info", "warning", "error"]


class ErrorDetail(_Model):
    """The object form of an HTTP ``detail`` on chat routes."""

    code: str
    message: str


def error_detail(code: ChatErrorCode | str, message: str) -> dict[str, str]:
    """``raise HTTPException(409, detail=error_detail(ChatErrorCode.busy, "..."))``"""
    return ErrorDetail(code=str(code), message=message).model_dump()


# ---------------------------------------------------------------------------
# Cards (B3 "Card shape")
# ---------------------------------------------------------------------------


class CardAction(_Model):
    """A button on a card. ``id`` is the :class:`ActionType` value the client
    submits to ``POST /chat/{sid}/actions`` when it is clicked (any payload is
    taken from the card's ``data`` by the card component)."""

    id: Annotated[str, Field(min_length=1, max_length=MAX_ID_LENGTH)]
    label: Annotated[str, Field(min_length=1, max_length=MAX_LABEL_LENGTH)]
    variant: CardActionVariant = "secondary"


class Card(_Model):
    """One structured UI card. ``data`` is plain JSON whose shape depends on
    ``type`` (defined by the card builders, ``services/chat/cards.py``).
    ``spec_version`` is the draft-spec version the card was built from; a card
    is rendered inert once ``status`` is not ``active``."""

    id: IdStr
    type: CardType
    status: CardStatus = CardStatus.active
    spec_version: Annotated[int, Field(ge=0)]
    title: str = ""
    data: JsonObject = Field(default_factory=dict)
    actions: list[CardAction] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Draft spec summary (B4) -- what the "Study spec" panel and `spec` event carry
# ---------------------------------------------------------------------------


class SpecField(_Model):
    path: Annotated[str, Field(min_length=1, max_length=MAX_ID_LENGTH * 2)]
    label: Annotated[str, Field(min_length=1, max_length=MAX_LABEL_LENGTH)]
    #: Human-readable value, or None when the field is still empty.
    display_value: str | None = None
    status: FieldStatus
    #: How the value got there; None while the field is empty.
    source: FieldSource | None = None


class SpecSummary(_Model):
    kind: SessionKind
    spec_version: Annotated[int, Field(ge=0)]
    #: True when the Generate / Create action is available.
    ready: bool
    #: ``SpecField.path`` of every field still required (a subset of ``fields``).
    missing: list[str] = Field(default_factory=list)
    fields: list[SpecField] = Field(default_factory=list)


class Guide(_Model):
    """Deterministic next-step line plus suggestion chips returned after each
    action, so card clicks never cost AI tokens."""

    text: str
    chips: list[str] = Field(default_factory=list)


class MessageFlags(_Model):
    """Per-message flags. ``unverified_numbers``: numbers in the assistant text
    that no tool result / spec / card / user message contains (B7); the UI shows
    an amber "check the cards" note when non-empty."""

    unverified_numbers: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Session / message models and route request-response models (B3)
# ---------------------------------------------------------------------------


class CreateChatRequest(_Request):
    kind: SessionKind
    #: protocol_edit: the target protocol (required). budget: protocol to link
    #: (optional). protocol: not allowed.
    protocol_id: UuidStr | None = None
    #: Stored as an unprocessed first user message; no model call is made.
    initial_message: InitialMessageText = None

    @model_validator(mode="after")
    def _check_protocol_id(self) -> "CreateChatRequest":
        if self.kind == SessionKind.protocol_edit and self.protocol_id is None:
            raise ValueError("protocol_id is required for protocol_edit sessions")
        if self.kind == SessionKind.protocol and self.protocol_id is not None:
            raise ValueError("protocol_id is not allowed for protocol sessions")
        return self


class ChatSessionRead(_Model):
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: IdStr
    kind: SessionKind
    status: SessionStatus
    title: str | None = None
    spec_version: Annotated[int, Field(ge=0)] = 0
    protocol_id: IdStr | None = None
    budget_id: IdStr | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    last_activity_at: UtcDatetime


class ChatSessionListItem(_Model):
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: IdStr
    kind: SessionKind
    status: SessionStatus
    title: str | None = None
    protocol_id: IdStr | None = None
    budget_id: IdStr | None = None
    created_at: UtcDatetime
    last_activity_at: UtcDatetime


class ChatMessageDisplay(_Model):
    """A transcript entry as the browser renders it (``chat_messages.display``
    plus row metadata). ``tool_results`` rows are never sent."""

    id: IdStr
    seq: Annotated[int, Field(ge=0)]
    role: MessageRole
    text: str
    card_ids: list[IdStr] = Field(default_factory=list)
    flags: MessageFlags = Field(default_factory=MessageFlags)
    created_at: UtcDatetime


class ChatSessionDetail(_Model):
    session: ChatSessionRead
    messages: list[ChatMessageDisplay] = Field(default_factory=list)
    cards: list[Card] = Field(default_factory=list)
    spec: SpecSummary
    #: True when the last transcript message is a user message with no reply yet
    #: (e.g. the stored initial_message): the client should POST a turn with
    #: ``resume: true``.
    awaiting_reply: bool = False


class TurnRequest(_Request):
    """Send exactly one of ``text`` (a new user message) or ``resume: true``
    (process the already-stored unanswered user message). ``client_turn_id`` is
    an idempotency key: retrying the same id never runs the turn twice."""

    text: TurnText = None
    resume: bool = False
    client_turn_id: ClientTurnId

    @model_validator(mode="after")
    def _text_xor_resume(self) -> "TurnRequest":
        if self.resume and self.text is not None:
            raise ValueError("send either text or resume=true, not both")
        if not self.resume and self.text is None:
            raise ValueError("text is required unless resume=true")
        return self


class PatchChatRequest(_Request):
    """Rename and/or archive. ``completed`` is server-controlled."""

    title: TitleText = None
    status: Literal["active", "archived"] | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "PatchChatRequest":
        if self.title is None and self.status is None:
            raise ValueError("provide title and/or status")
        return self


# ---------------------------------------------------------------------------
# Actions (B3): deterministic card clicks, no AI
# ---------------------------------------------------------------------------


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SelectDrugPayload(_Payload):
    drug_id: UuidStr


class AddDrugPayload(_Payload):
    inn: InnStr


class SetStudyTypePayload(_Payload):
    study_type: StudyTypeLiteral


class SetGuidelinePayload(_Payload):
    guideline_version: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_GUIDELINE_VERSION_LENGTH)
    ]


class UpdatePopulationPayload(_Payload):
    #: Partial PopulationConfig; keys/ranges are validated by the spec layer.
    patch: Annotated[dict[str, Any], Field(min_length=1)]


class ToggleExclusionPayload(_Payload):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_EXCLUSION_TEXT)]
    on: bool


class SetDesignDecisionPayload(_Payload):
    key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_DESIGN_KEY_LENGTH)]
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_DESIGN_VALUE_LENGTH)]


class SetIndicationPayload(_Payload):
    #: Required key; ``null`` clears the user-supplied indication.
    text: IndicationText


class EmptyPayload(_Payload):
    """For actions that take no arguments (confirm_design, generate_protocol, create_budget)."""


class SetBudgetFieldsPayload(_Payload):
    #: Partial BudgetCreate fields; validated by the budget spec layer.
    patch: Annotated[dict[str, Any], Field(min_length=1)]


class LinkProtocolPayload(_Payload):
    #: Required key; ``null`` unlinks.
    protocol_id: UuidStr | None


class ProposalActionPayload(_Payload):
    proposal_id: UuidStr


ACTION_PAYLOAD_MODELS: dict[ActionType, type[_Payload]] = {
    ActionType.select_drug: SelectDrugPayload,
    ActionType.add_drug: AddDrugPayload,
    ActionType.set_study_type: SetStudyTypePayload,
    ActionType.set_guideline: SetGuidelinePayload,
    ActionType.update_population: UpdatePopulationPayload,
    ActionType.toggle_exclusion: ToggleExclusionPayload,
    ActionType.set_design_decision: SetDesignDecisionPayload,
    ActionType.set_indication: SetIndicationPayload,
    ActionType.confirm_design: EmptyPayload,
    ActionType.generate_protocol: EmptyPayload,
    ActionType.set_budget_fields: SetBudgetFieldsPayload,
    ActionType.link_protocol: LinkProtocolPayload,
    ActionType.create_budget: EmptyPayload,
    ActionType.confirm_edit: ProposalActionPayload,
    ActionType.apply_edit: ProposalActionPayload,
    ActionType.discard_edit: ProposalActionPayload,
}

#: Which session kinds may submit each action (anything else -> 422 invalid_action).
ACTION_SESSION_KINDS: dict[ActionType, frozenset[SessionKind]] = {
    **{
        a: frozenset({SessionKind.protocol})
        for a in (
            ActionType.select_drug,
            ActionType.add_drug,
            ActionType.set_study_type,
            ActionType.set_guideline,
            ActionType.update_population,
            ActionType.toggle_exclusion,
            ActionType.set_design_decision,
            ActionType.set_indication,
            ActionType.confirm_design,
            ActionType.generate_protocol,
        )
    },
    **{
        a: frozenset({SessionKind.budget})
        for a in (ActionType.set_budget_fields, ActionType.link_protocol, ActionType.create_budget)
    },
    **{
        a: frozenset({SessionKind.protocol_edit})
        for a in (ActionType.confirm_edit, ActionType.apply_edit, ActionType.discard_edit)
    },
}

#: Actions that spend credits and so also need an active license (B3 gates).
ACTIONS_REQUIRING_LICENSE: frozenset[ActionType] = frozenset(
    {ActionType.generate_protocol, ActionType.confirm_edit}
)


class ActionRequest(_Request):
    card_id: UuidStr
    action: ActionType
    payload: dict[str, Any] = Field(default_factory=dict)
    #: The spec version the client last saw; a mismatch is 409 ``stale_spec``.
    spec_version: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _payload_matches_action(self) -> "ActionRequest":
        # Validate eagerly so a bad payload is a 422 before any work happens.
        try:
            ACTION_PAYLOAD_MODELS[self.action].model_validate(self.payload)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'payload'}: {err['msg']}"
                for err in exc.errors(include_url=False, include_input=False)
            )
            raise ValueError(f"invalid payload for {self.action.value}: {problems}") from None
        return self

    def parsed_payload(self) -> _Payload:
        """The payload as its typed model (e.g. ``SelectDrugPayload``)."""
        return ACTION_PAYLOAD_MODELS[self.action].model_validate(self.payload)


class ActionResponse(_Model):
    spec_version: Annotated[int, Field(ge=0)]
    spec: SpecSummary
    #: Cards created OR changed by the action (new cards, plus any whose status
    #: moved to superseded/done), so the client can upsert them by id.
    cards: list[Card] = Field(default_factory=list)
    guide: Guide

    @model_validator(mode="after")
    def _versions_agree(self) -> "ActionResponse":
        if self.spec_version != self.spec.spec_version:
            raise ValueError("spec_version must equal spec.spec_version")
        return self


# ---------------------------------------------------------------------------
# Edit proposals (B9): GET /chat/{sid}/proposals/{pid}
# ---------------------------------------------------------------------------


class SectionDiffItem(_Model):
    """Mirror of ``schemas.SectionDiff`` (so ``SectionDiff.model_dump()`` fits)."""

    section_name: str
    display_name: str
    status: Literal["unchanged", "modified", "added", "removed"]
    old_content: str | None = None
    new_content: str | None = None
    diff_lines: list[str] = Field(default_factory=list)
    word_count_delta: int = 0


class ProposalRead(_Model):
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    id: IdStr
    protocol_id: IdStr
    session_id: IdStr | None = None
    kind: ProposalKind
    status: ProposalStatus
    base_version: Annotated[int, Field(ge=0)]
    applied_version: int | None = None
    #: What was asked for (inputs / sections / instruction).
    request: JsonObject = Field(default_factory=dict)
    #: Present once ``status == ready``: per-section diff, QC findings and the
    #: locked facts before/after (engine edits).
    diff: list[SectionDiffItem] = Field(default_factory=list)
    qc: JsonObject | None = None
    facts_before: JsonObject | None = None
    facts_after: JsonObject | None = None
    error: str | None = None
    started_at: UtcDatetime | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime


# ---------------------------------------------------------------------------
# SSE events (B3 "Stream format")
# ---------------------------------------------------------------------------


class SseEventName(StrEnum):
    turn_start = "turn_start"
    text_delta = "text_delta"
    tool_start = "tool_start"
    card = "card"
    spec = "spec"
    notice = "notice"
    turn_end = "turn_end"
    error = "error"
    #: GET /chat/stream-check only.
    tick = "tick"


class TurnStartEvent(_Model):
    turn_id: str


class TextDeltaEvent(_Model):
    text: str


class ToolStartEvent(_Model):
    tool: str
    #: Human-readable progress line, e.g. "Checking the DailyMed label...".
    label: str


# The `card` event's data is a bare Card (no wrapper).
CardEvent = Card


class SpecEvent(_Model):
    spec_version: Annotated[int, Field(ge=0)]
    summary: SpecSummary


class NoticeEvent(_Model):
    level: NoticeLevel
    text: str


class TurnEndEvent(_Model):
    message_id: str
    #: The model's stop reason verbatim (end_turn, tool_use, max_tokens,
    #: pause_turn, refusal, stop_sequence, ...) or a server value such as
    #: ``iteration_limit``. Left open: new values appear without a contract change.
    stop_reason: str
    flags: MessageFlags = Field(default_factory=MessageFlags)


class ErrorEvent(_Model):
    #: A :class:`ChatErrorCode` value (typed ``str`` so an unexpected code can
    #: never break the error path itself).
    code: str
    message: str
    retryable: bool = False


class StreamCheckTick(_Model):
    seq: Annotated[int, Field(ge=1)]
    total: Annotated[int, Field(ge=1)]
    #: Milliseconds since the stream opened, measured on the server.
    elapsed_ms: Annotated[int, Field(ge=0)]


#: Event name -> payload model; ``format_sse`` enforces the pairing.
SSE_EVENT_MODELS: dict[SseEventName, type[BaseModel]] = {
    SseEventName.turn_start: TurnStartEvent,
    SseEventName.text_delta: TextDeltaEvent,
    SseEventName.tool_start: ToolStartEvent,
    SseEventName.card: Card,
    SseEventName.spec: SpecEvent,
    SseEventName.notice: NoticeEvent,
    SseEventName.turn_end: TurnEndEvent,
    SseEventName.error: ErrorEvent,
    SseEventName.tick: StreamCheckTick,
}


def format_sse(event: str, payload: BaseModel) -> str:
    """One SSE frame: ``event: <name>\\ndata: <json>\\n\\n``.

    The JSON is compact and single-line (newlines inside strings are escaped),
    so it is always exactly one ``data:`` line. Raises ``ValueError`` for an
    unknown event name and ``TypeError`` if ``payload`` is not the model the
    contract pairs with that event -- wiring mistakes fail loudly in tests
    instead of producing a frame the browser cannot parse.
    """
    try:
        name = SseEventName(event)
    except ValueError:
        raise ValueError(f"unknown SSE event {event!r}") from None
    expected = SSE_EVENT_MODELS[name]
    if not isinstance(payload, expected):
        raise TypeError(
            f"event {name.value!r} requires {expected.__name__}, got {type(payload).__name__}"
        )
    return f"event: {name.value}\ndata: {payload.model_dump_json()}\n\n"


# ===========================================================================
# Card ``data`` models (wave 2, B3 "Chat core") -- APPENDED, nothing above is
# changed.
#
# ``Card.data`` is plain JSON whose shape depends on ``Card.type``. These models
# are that shape, one per :class:`CardType`; the card builders in
# ``services/chat/cards.py`` construct them and ``model_dump(mode="json")`` them
# into ``Card.data``. The TypeScript mirror (frontend/src/chat/types.ts) should
# copy them field for field. Optional fields are always PRESENT in the JSON
# (``null`` when unset) because the builders dump without ``exclude_none``.
#
# Button payloads: a card's ``actions`` (CardAction) are its primary buttons;
# the payload each action submits is fixed by the action type and the data
# below, so the card component never has to invent one:
#
#   drug_matches        per match           -> select_drug      {drug_id: match.id}
#   drug_add_offer      action add_drug     -> add_drug         {inn: data.inn}
#   study_type          per allowed option  -> set_study_type   {study_type: option.value}
#   population          Save                -> update_population {patch: {...changed keys}}
#                       "Looks good"        -> update_population {patch: data.confirm_patch}
#   cv_override_warning action              -> update_population {patch: data.clear_patch}
#   interaction_screening per row toggle    -> toggle_exclusion {text: row.criterion_text, on}
#   design_decisions    per option select   -> set_design_decision {key, value}
#                       action confirm_design  -> confirm_design {}
#   indication          per candidate       -> set_indication   {text: candidate.indication}
#                       action set_indication -> set_indication {text: null}  ("let the engine choose")
#   guideline           per option          -> set_guideline    {guideline_version: option.id}
#   protocol_summary    action              -> generate_protocol {}
#   budget_summary      per protocol option -> link_protocol    {protocol_id: option.id}
#                       "unlink"            -> link_protocol    {protocol_id: null}
#                       action              -> create_budget    {}
#   edit_impact         actions             -> confirm_edit / discard_edit {proposal_id}
#   edit_diff           actions             -> apply_edit / discard_edit   {proposal_id}
#
# protocol_ready, budget_ready, protocol_overview, generation_progress,
# budget_preview, edit_progress and notice have no server action; any buttons
# on them ("Open in editor", "Price this study") are client navigations built
# from the ids in ``data``.
# ===========================================================================

Modality = Literal["biologic", "small_molecule", "unknown"]


class FactRow(_Model):
    """A label/value pair (summary rows, locked facts, estimates)."""

    label: str
    value: str


class SummaryRow(_Model):
    """One row of a protocol / budget summary card."""

    #: SpecField.path the row summarises.
    path: str
    label: str
    value: str | None = None
    status: FieldStatus
    source: FieldSource | None = None
    #: True while the value is only ``proposed`` (set by the assistant, not yet
    #: confirmed by a click or a verified quote): the card highlights it.
    needs_check: bool = False


# -- drug_matches / drug_add_offer ------------------------------------------


class DrugMatchItem(_Model):
    id: str
    inn: str
    brand_names: list[str] = Field(default_factory=list)
    modality: Modality = "unknown"
    #: Study types this drug may run; empty when the modality is unknown (unrestricted).
    allowed_study_types: list[str] = Field(default_factory=list)


class DrugMatchesCardData(_Model):
    query: str
    matches: list[DrugMatchItem] = Field(default_factory=list)
    #: True when more catalogue rows matched than are listed.
    truncated: bool = False
    #: Drug id currently chosen in the spec, if any (the card then shows it as selected).
    selected_drug_id: str | None = None


class DrugAddOfferCardData(_Model):
    query: str
    #: Normalised INN the add_drug action would send; null when ``query`` is not a valid INN.
    inn: str | None = None
    valid: bool
    message: str
    modality: Modality = "unknown"
    allowed_study_types: list[str] = Field(default_factory=list)


# -- study_type ---------------------------------------------------------------


class StudyTypeOption(_Model):
    value: StudyTypeLiteral
    label: str
    allowed: bool
    #: Why a disallowed option is disabled (the server's own gate wording).
    reason: str | None = None


class StudyTypeCardData(_Model):
    drug_inn: str | None = None
    modality: Modality = "unknown"
    options: list[StudyTypeOption] = Field(default_factory=list)
    selected: StudyTypeLiteral | None = None
    #: Set when an attempted choice was refused (shown above the options).
    gate_message: str | None = None


# -- population / cv_override_warning ----------------------------------------


class PopulationValues(_Model):
    """Mirror of the wizard's ``PopulationConfig`` (frontend/src/types.ts)."""

    subject_type: Literal["healthy", "patient"] = "healthy"
    age_min: int = 18
    age_max: int = 55
    sex: Literal["male", "female", "both"] = "both"
    weight_min: int = 50
    weight_max: int = 100
    exclusion_presets: list[str] = Field(default_factory=list)
    cv_intra: float | None = None
    dose_strength: str | None = None
    reference_product: str | None = None
    is_nti: bool | None = None
    additional_pk_timepoints_hours: str | None = None
    additional_immunogenicity_timepoints_months: str | None = None


class PopulationRanges(_Model):
    """The UI / chat validation ranges (the server itself accepts any value)."""

    age_min: int = 18
    age_max: int = 80
    weight_min: int = 40
    weight_max: int = 150
    cv_min: float = 1.0
    cv_max: float = 120.0


class PopulationCardData(_Model):
    drug_inn: str | None = None
    study_type: StudyTypeLiteral | None = None
    values: PopulationValues
    ranges: PopulationRanges = Field(default_factory=PopulationRanges)
    #: Drug-agnostic exclusion criteria offered as toggles (drug-specific ones
    #: come from the interaction_screening card).
    generic_exclusion_options: list[str] = Field(default_factory=list)
    #: ``SpecField.path`` -> FieldStatus value, for the fields shown.
    field_status: dict[str, str] = Field(default_factory=dict)
    #: Patch for the "Looks good" quick-confirm (an ``update_population`` payload).
    confirm_patch: dict[str, Any] = Field(default_factory=dict)


class CvOverrideWarningCardData(_Model):
    cv_intra: float
    message: str
    consequences: list[str] = Field(default_factory=list)
    #: ``update_population`` patch that removes the override: ``{"cv_intra": null}``.
    clear_patch: dict[str, Any] = Field(default_factory=dict)


# -- interaction_screening ----------------------------------------------------


class DdiRowData(_Model):
    key: str
    label: str
    #: "prohibited" | "restricted" | "permitted" (open string: engine-owned).
    status: str
    pathway: str | None = None
    examples: list[str] = Field(default_factory=list)
    washout_days: float | None = None
    washout_basis: str | None = None
    history_exclusion: bool = False
    rationale: str | None = None
    conditions: str | None = None
    escalated_by_nti: bool = False
    assumption: str | None = None
    #: Matched DailyMed label text backing the row (display as quoted data).
    evidence_quote: str | None = None
    #: The exclusion-criterion sentence the toggle adds (same wording rule as the wizard).
    criterion_text: str
    #: True when ``criterion_text`` is currently in the spec's exclusion list.
    selected: bool = False


class InteractionScreeningCardData(_Model):
    drug_inn: str | None = None
    design_key: str | None = None
    is_nti: bool | None = None
    pathway_keys: list[str] = Field(default_factory=list)
    prohibited: list[DdiRowData] = Field(default_factory=list)
    restricted: list[DdiRowData] = Field(default_factory=list)
    permitted: list[DdiRowData] = Field(default_factory=list)
    max_washout_days: float | None = None
    screening_history_window_days: float | None = None
    #: Number of unresolved-pathway notes (engine fell back to a pan-pathway prohibition).
    unresolved_count: int = 0
    summary: str | None = None


# -- design_decisions ---------------------------------------------------------


class DesignDecisionData(_Model):
    key: str
    label: str
    value: str
    rationale: str = ""
    evidence: str = ""
    options: list[str] = Field(default_factory=list)


class DesignDecisionsCardData(_Model):
    drug_inn: str | None = None
    study_type: StudyTypeLiteral | None = None
    decisions: list[DesignDecisionData] = Field(default_factory=list)
    #: True once the user clicked "Confirm design" for these decisions.
    confirmed: bool = False


# -- indication ---------------------------------------------------------------


class IndicationCandidateData(_Model):
    indication: str
    on_label: bool = False
    label_evidence: str | None = None
    therapeutic_area: str | None = None
    setting_note: str | None = None
    monotherapy: str | None = None
    pk_variability: str | None = None
    observed_cv_pct: float | None = None
    recruitment: str | None = None
    rank_reason: str | None = None


class IndicationCardData(_Model):
    #: False when the study is not a patient biosimilar study (the engine says why in ``reason``).
    applicable: bool
    reason: str | None = None
    drug_inn: str | None = None
    drug_class: str | None = None
    label_verified: bool | None = None
    recommended: IndicationCandidateData | None = None
    ranked: list[IndicationCandidateData] = Field(default_factory=list)
    user_indication: str | None = None
    user_indication_warnings: list[str] = Field(default_factory=list)
    #: The indication currently chosen in the spec (null = the engine chooses).
    selected_indication: str | None = None
    summary: str | None = None


# -- guideline ----------------------------------------------------------------


class GuidelineOptionData(_Model):
    id: str
    display_name: str
    regulator: str
    region: str
    citation: str
    is_current: bool = True
    submission_pathway: str = ""


class GuidelineCardData(_Model):
    selected: str
    default: str = DEFAULT_GUIDELINE_VERSION
    options: list[GuidelineOptionData] = Field(default_factory=list)


# -- protocol_summary / generation_progress / protocol_ready -------------------


class ProtocolSummaryCardData(_Model):
    rows: list[SummaryRow] = Field(default_factory=list)
    ready: bool
    #: Labels of what is still required (empty when ready).
    missing: list[str] = Field(default_factory=list)
    #: A server refusal that blocks Generate even though the spec is complete
    #: (e.g. the study-type gate); the server's own wording.
    blocking_issue: str | None = None
    notes: list[str] = Field(default_factory=list)


class GenerationProgressCardData(_Model):
    protocol_id: str
    drug_inn: str | None = None
    #: "generating" | "complete" | "failed" (Protocol.generation_status).
    status: str = "generating"
    error: str | None = None


class ProtocolReadyCardData(_Model):
    protocol_id: str
    drug_inn: str | None = None
    study_type: str | None = None
    guideline_version: str | None = None
    section_count: int = 0
    version: int = 1


# -- budget_preview / budget_summary / budget_ready ---------------------------


class BudgetDefaultsData(_Model):
    n_enrolled: int | None = None
    n_sites: int | None = None
    n_countries: int | None = None
    project_duration_months: float | None = None
    startup_months: float | None = None
    recruitment_months: float | None = None
    treatment_months: float | None = None
    followup_months: float | None = None
    closeout_months: float | None = None
    n_expected_saes: int | None = None
    geography_profile: str | None = None


class BudgetPreviewCardData(_Model):
    """Projection of ``BudgetProfilePreview`` (the engine's resolved profile)."""

    drug_inn: str
    modality: Modality = "unknown"
    allowed_study_types: list[str] = Field(default_factory=list)
    study_type: str = ""
    subject_profile: str
    subject_profile_source: str = ""
    subject_profile_basis: str = ""
    therapeutic_area: str = "unknown"
    profile_key: str | None = None
    defaults: BudgetDefaultsData = Field(default_factory=BudgetDefaultsData)
    estimated_grand_total: float
    currency: str = "USD"
    category_count: int = 0
    disclaimer: str = ""


class ProtocolOption(_Model):
    id: str
    drug_inn: str
    study_type: str
    status: str
    version: int
    created_at: str | None = None


class BudgetSummaryCardData(_Model):
    rows: list[SummaryRow] = Field(default_factory=list)
    ready: bool
    missing: list[str] = Field(default_factory=list)
    blocking_issue: str | None = None
    #: The protocol currently linked in the spec, if any.
    linked_protocol: ProtocolOption | None = None
    #: Org protocols that can be linked (empty unless the assistant listed them).
    protocol_options: list[ProtocolOption] = Field(default_factory=list)
    #: Engine's estimate for the current drug (null until a preview ran).
    estimated_grand_total: float | None = None
    currency: str = "USD"
    notes: list[str] = Field(default_factory=list)


class BudgetReadyCardData(_Model):
    budget_id: str
    name: str
    drug_inn: str | None = None
    study_type: str | None = None
    currency: str = "USD"
    grand_total: float | None = None
    category_count: int = 0
    n_enrolled: int | None = None
    protocol_id: str | None = None


# -- protocol_overview / edit_impact / edit_progress / edit_diff ---------------


class SectionStat(_Model):
    name: str
    label: str
    words: int


class ProtocolOverviewCardData(_Model):
    protocol_id: str
    drug_inn: str | None = None
    status: str
    version: int
    study_type: str
    guideline_version: str
    generation_status: str = "complete"
    #: True when chat edits are allowed (status == draft and generation complete).
    editable: bool
    blocked_reason: str | None = None
    sections: list[SectionStat] = Field(default_factory=list)
    #: Engine-locked facts (N, design, CV and its source, decisions, DDI rows...).
    locked_facts: list[FactRow] = Field(default_factory=list)


class EditChange(_Model):
    field: str
    label: str
    before: str | None = None
    after: str | None = None


class EditEstimate(_Model):
    label: str
    rows: list[FactRow] = Field(default_factory=list)
    note: str = ""


class EditImpactCardData(_Model):
    #: Pre-minted id of the proposal the confirm_edit action will create.
    proposal_id: str
    kind: ProposalKind
    base_version: int
    editable: bool = True
    blocked_reason: str | None = None
    #: engine edits: the inputs that change.
    changes: list[EditChange] = Field(default_factory=list)
    #: prose edits: the section names to rewrite and the instruction.
    sections: list[str] = Field(default_factory=list)
    #: display titles for ``sections`` (section key -> the protocol's own title).
    section_labels: dict[str, str] = Field(default_factory=dict)
    instruction: str | None = None
    estimate: EditEstimate | None = None
    warnings: list[str] = Field(default_factory=list)
    rerun_note: str = ""


class EditProgressCardData(_Model):
    proposal_id: str
    kind: ProposalKind
    status: ProposalStatus = ProposalStatus.generating
    error: str | None = None


class EditDiffCardData(_Model):
    proposal_id: str
    kind: ProposalKind
    status: ProposalStatus = ProposalStatus.ready
    base_version: int
    #: True when the protocol changed after the proposal was made (Apply is blocked).
    stale: bool = False
    sections: list[SectionDiffItem] = Field(default_factory=list)
    qc: JsonObject | None = None
    facts_before: list[FactRow] = Field(default_factory=list)
    facts_after: list[FactRow] = Field(default_factory=list)
    applied_version: int | None = None


# -- notice -------------------------------------------------------------------


class NoticeCardData(_Model):
    level: NoticeLevel = "info"
    text: str
    code: str | None = None


#: CardType -> the model of its ``data`` (the builders in cards.py validate against it).
CARD_DATA_MODELS: dict[CardType, type[_Model]] = {
    CardType.drug_matches: DrugMatchesCardData,
    CardType.drug_add_offer: DrugAddOfferCardData,
    CardType.study_type: StudyTypeCardData,
    CardType.population: PopulationCardData,
    CardType.cv_override_warning: CvOverrideWarningCardData,
    CardType.interaction_screening: InteractionScreeningCardData,
    CardType.design_decisions: DesignDecisionsCardData,
    CardType.indication: IndicationCardData,
    CardType.guideline: GuidelineCardData,
    CardType.protocol_summary: ProtocolSummaryCardData,
    CardType.generation_progress: GenerationProgressCardData,
    CardType.protocol_ready: ProtocolReadyCardData,
    CardType.budget_preview: BudgetPreviewCardData,
    CardType.budget_summary: BudgetSummaryCardData,
    CardType.budget_ready: BudgetReadyCardData,
    CardType.protocol_overview: ProtocolOverviewCardData,
    CardType.edit_impact: EditImpactCardData,
    CardType.edit_progress: EditProgressCardData,
    CardType.edit_diff: EditDiffCardData,
    CardType.notice: NoticeCardData,
}
