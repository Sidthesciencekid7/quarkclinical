"""Pure tests for the chat wire contract (services/chat/contract.py).

No database, no network, no fixtures: this file must stay runnable without the
test database. It also guards the TypeScript mirror (frontend/src/chat/types.ts)
against drift from the Python enums and limits.
"""

import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from services.chat import contract as c

BACKEND_DIR = Path(__file__).resolve().parent.parent
TYPES_TS = BACKEND_DIR.parent / "frontend" / "src" / "chat" / "types.ts"


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums: exactly the planned values (docs/redesign-chat-plan.md B2 / B3)
# ---------------------------------------------------------------------------

PLANNED_ENUMS = [
    (c.SessionKind, ["protocol", "budget", "protocol_edit"]),
    (c.SessionStatus, ["active", "completed", "archived"]),
    (c.MessageRole, ["user", "assistant", "tool_results", "ui_action", "system_note"]),
    (
        c.CardType,
        [
            "drug_matches", "drug_add_offer", "study_type", "population", "cv_override_warning",
            "interaction_screening", "design_decisions", "indication", "guideline",
            "protocol_summary", "generation_progress", "protocol_ready",
            "budget_preview", "budget_summary", "budget_ready",
            "protocol_overview", "edit_impact", "edit_progress", "edit_diff",
            "notice",
        ],
    ),
    (c.CardStatus, ["active", "superseded", "done", "error"]),
    (
        c.ActionType,
        [
            "select_drug", "add_drug", "set_study_type", "set_guideline", "update_population",
            "toggle_exclusion", "set_design_decision", "set_indication", "confirm_design",
            "generate_protocol", "set_budget_fields", "link_protocol", "create_budget",
            "confirm_edit", "apply_edit", "discard_edit",
        ],
    ),
    (c.FieldStatus, ["empty", "default", "engine", "proposed", "confirmed"]),
    (c.FieldSource, ["default", "engine", "user_click", "user_text"]),
    (c.ProposalKind, ["prose", "engine"]),
    (c.ProposalStatus, ["proposed", "generating", "ready", "applied", "discarded", "failed", "stale"]),
    (
        c.SseEventName,
        ["turn_start", "text_delta", "tool_start", "card", "spec", "notice", "turn_end", "error", "tick"],
    ),
]


@pytest.mark.parametrize("enum_cls,expected", PLANNED_ENUMS, ids=[e.__name__ for e, _ in PLANNED_ENUMS])
def test_enum_contains_exactly_the_planned_values(enum_cls, expected):
    assert [m.value for m in enum_cls] == expected
    # name == value, and members are plain strings on the wire
    assert all(m.name == m.value for m in enum_cls)
    assert all(isinstance(m, str) for m in enum_cls)


def test_card_action_variant_literals():
    from typing import get_args

    assert get_args(c.CardActionVariant) == ("primary", "secondary", "ghost", "danger")
    assert get_args(c.NoticeLevel) == ("info", "warning", "error")
    assert get_args(c.StudyTypeLiteral) == ("BE", "biosimilar_pk", "food_effect")


def test_limits_constants():
    assert c.MAX_TURN_TEXT == 4000
    assert c.MAX_INITIAL_MESSAGE == 4000
    assert c.MAX_TITLE == 200
    assert c.MAX_CLIENT_TURN_ID == 64
    assert c.CHAT_DAILY_TURN_CAP == 200
    assert c.CHAT_SESSION_TURN_CAP == 80
    assert c.CHAT_MAX_TOOL_ITERATIONS == 8
    assert c.SSE_HEARTBEAT_SECONDS == 15
    assert c.STREAM_CHECK_EVENTS == 5
    assert c.DEFAULT_GUIDELINE_VERSION == "FDA-BE-2014"
    assert c.SSE_PING == ": ping\n\n"


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def _card(**over) -> c.Card:
    base = dict(
        id=_uid(),
        type="design_decisions",
        status="active",
        spec_version=7,
        title="Study design",
        data={"decisions": [{"key": "population", "value": "healthy", "options": ["healthy", "patients"]}], "n": 3.5},
        actions=[{"id": "confirm_design", "label": "Confirm design", "variant": "primary"}],
    )
    base.update(over)
    return c.Card(**base)


def test_card_round_trips_json():
    card = _card()
    wire = card.model_dump_json()
    again = c.Card.model_validate_json(wire)
    assert again == card
    parsed = json.loads(wire)
    assert set(parsed) == {"id", "type", "status", "spec_version", "title", "data", "actions"}
    assert parsed["type"] == "design_decisions"
    assert parsed["status"] == "active"
    assert parsed["actions"] == [{"id": "confirm_design", "label": "Confirm design", "variant": "primary"}]
    assert parsed["data"]["n"] == 3.5


def test_card_defaults():
    card = c.Card(id=_uid(), type="notice", spec_version=0)
    assert card.status == c.CardStatus.active
    assert card.title == ""
    assert card.data == {}
    assert card.actions == []


def test_card_action_variant_defaults_to_secondary_and_rejects_unknown():
    assert c.CardAction(id="x", label="X").variant == "secondary"
    with pytest.raises(ValidationError):
        c.CardAction(id="x", label="X", variant="loud")
    with pytest.raises(ValidationError):
        c.CardAction(id="", label="X")
    with pytest.raises(ValidationError):
        c.CardAction(id="x", label="")


def test_card_rejects_unknown_type_status_and_negative_version():
    with pytest.raises(ValidationError):
        _card(type="wizard_step")
    with pytest.raises(ValidationError):
        _card(status="pending")
    with pytest.raises(ValidationError):
        _card(spec_version=-1)
    with pytest.raises(ValidationError):
        _card(id="")


def test_card_data_is_normalised_to_plain_json():
    some_id = uuid.uuid4()
    when = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    card = _card(data={"id": some_id, "when": when, "pair": (1, 2), "kind": c.SessionKind.budget})
    assert card.data == {"id": str(some_id), "when": when.isoformat().replace("+00:00", "Z"), "pair": [1, 2], "kind": "budget"}
    json.dumps(card.data, allow_nan=False)  # genuinely plain JSON


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_card_data_rejects_non_finite_numbers(bad):
    with pytest.raises(ValidationError):
        _card(data={"cv": bad})


def test_card_data_rejects_non_serialisable_objects():
    with pytest.raises(ValidationError):
        _card(data={"x": object()})


def test_card_accepts_uuid_objects_for_ids():
    uid = uuid.uuid4()
    card = c.Card(id=uid, type="notice", spec_version=1)
    assert card.id == str(uid)
    assert json.loads(card.model_dump_json())["id"] == str(uid)


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------

EVENT_EXAMPLES: dict[str, BaseModel] = {
    "turn_start": c.TurnStartEvent(turn_id="t-1"),
    "text_delta": c.TextDeltaEvent(text="Hello"),
    "tool_start": c.ToolStartEvent(tool="screen_interactions", label="Checking the DailyMed label..."),
    "card": _card(),
    "spec": c.SpecEvent(
        spec_version=2,
        summary=c.SpecSummary(kind="protocol", spec_version=2, ready=False, missing=["drug"], fields=[]),
    ),
    "notice": c.NoticeEvent(level="warning", text="Heads up"),
    "turn_end": c.TurnEndEvent(message_id=_uid(), stop_reason="end_turn", flags=c.MessageFlags(unverified_numbers=["42"])),
    "error": c.ErrorEvent(code="busy", message="Another reply is in progress.", retryable=True),
    "tick": c.StreamCheckTick(seq=1, total=5, elapsed_ms=0),
}


def test_every_sse_event_has_a_model_and_an_example():
    assert set(c.SSE_EVENT_MODELS) == set(c.SseEventName)
    assert set(EVENT_EXAMPLES) == {e.value for e in c.SseEventName}
    for name, payload in EVENT_EXAMPLES.items():
        assert isinstance(payload, c.SSE_EVENT_MODELS[c.SseEventName(name)])


@pytest.mark.parametrize("name", list(EVENT_EXAMPLES))
def test_format_sse_shape(name):
    payload = EVENT_EXAMPLES[name]
    frame = c.format_sse(name, payload)

    assert frame.endswith("\n\n") and not frame.endswith("\n\n\n")
    lines = frame[:-2].split("\n")
    assert len(lines) == 2
    assert lines[0] == f"event: {name}"
    assert lines[1].startswith("data: ")
    # the data line is the payload's JSON, and parses back to the same model
    assert json.loads(lines[1][len("data: "):]) == json.loads(payload.model_dump_json())
    assert type(payload).model_validate_json(lines[1][len("data: "):]) == payload


def test_format_sse_accepts_enum_member_as_event_name():
    frame = c.format_sse(c.SseEventName.text_delta, c.TextDeltaEvent(text="x"))
    assert frame == 'event: text_delta\ndata: {"text":"x"}\n\n'


def test_format_sse_data_is_always_one_line():
    nasty = "line1\nline2\r\nline3\rlast   sep"
    frame = c.format_sse("text_delta", c.TextDeltaEvent(text=nasty))
    body = frame[:-2]
    assert "\n" not in body.split("data: ", 1)[1]
    assert "\r" not in frame
    assert json.loads(body.split("data: ", 1)[1])["text"] == nasty


def test_format_sse_card_payload_is_a_bare_card():
    card = _card()
    data = json.loads(c.format_sse("card", card).split("data: ", 1)[1])
    assert data["id"] == card.id and data["type"] == "design_decisions"
    assert "card" not in data


def test_format_sse_rejects_unknown_event_and_mismatched_payload():
    with pytest.raises(ValueError):
        c.format_sse("heartbeat", c.TextDeltaEvent(text="x"))
    with pytest.raises(TypeError):
        c.format_sse("card", c.TextDeltaEvent(text="x"))
    with pytest.raises(TypeError):
        c.format_sse("text_delta", _card())


def test_turn_end_json_shape():
    payload = json.loads(c.TurnEndEvent(message_id="m1", stop_reason="end_turn").model_dump_json())
    assert payload == {"message_id": "m1", "stop_reason": "end_turn", "flags": {"unverified_numbers": []}}


def test_error_event_defaults_to_not_retryable_and_accepts_unknown_codes():
    assert c.ErrorEvent(code="whatever", message="m").retryable is False


def test_ping_is_an_sse_comment():
    assert c.SSE_PING.startswith(":")
    assert c.SSE_PING.endswith("\n\n")


# ---------------------------------------------------------------------------
# Request models: limits enforced
# ---------------------------------------------------------------------------


def test_turn_text_limit_is_4000_characters():
    assert c.TurnRequest(text="x" * 4000, client_turn_id="a").text == "x" * 4000
    with pytest.raises(ValidationError):
        c.TurnRequest(text="x" * 4001, client_turn_id="a")


def test_turn_text_is_stripped_before_the_limit_applies():
    assert c.TurnRequest(text="  " + "x" * 4000 + "  \n", client_turn_id="a").text == "x" * 4000


def test_turn_requires_exactly_one_of_text_or_resume():
    assert c.TurnRequest(text="hi", client_turn_id="a").resume is False
    assert c.TurnRequest(resume=True, client_turn_id="a").text is None
    with pytest.raises(ValidationError):
        c.TurnRequest(client_turn_id="a")
    with pytest.raises(ValidationError):
        c.TurnRequest(text="   ", client_turn_id="a")
    with pytest.raises(ValidationError):
        c.TurnRequest(text="hi", resume=True, client_turn_id="a")


def test_resume_with_blank_text_is_just_a_resume():
    assert c.TurnRequest(text="   ", resume=True, client_turn_id="a").text is None


@pytest.mark.parametrize("good", ["a", "x" * 64, "3f2b8c1e-aaaa-bbbb-cccc-0123456789ab", "turn_1:retry.2"])
def test_client_turn_id_accepts(good):
    assert c.TurnRequest(text="hi", client_turn_id=good).client_turn_id == good


@pytest.mark.parametrize("bad", ["", "x" * 65, "has space", "new\nline", "semi;colon", "slash/slash"])
def test_client_turn_id_rejects(bad):
    with pytest.raises(ValidationError):
        c.TurnRequest(text="hi", client_turn_id=bad)


def test_client_turn_id_is_required():
    with pytest.raises(ValidationError):
        c.TurnRequest(text="hi")


def test_requests_reject_unknown_fields():
    with pytest.raises(ValidationError):
        c.TurnRequest(text="hi", client_turn_id="a", model="gpt")
    with pytest.raises(ValidationError):
        c.CreateChatRequest(kind="budget", surprise=1)
    with pytest.raises(ValidationError):
        c.PatchChatRequest(title="x", archived=True)


def test_initial_message_limit_and_blank_handling():
    ok = c.CreateChatRequest(kind="protocol", initial_message="y" * 4000)
    assert len(ok.initial_message) == 4000
    with pytest.raises(ValidationError):
        c.CreateChatRequest(kind="protocol", initial_message="y" * 4001)
    assert c.CreateChatRequest(kind="protocol", initial_message="   ").initial_message is None
    assert c.CreateChatRequest(kind="protocol").initial_message is None


def test_create_chat_kind_and_protocol_id_rules():
    pid = _uid()
    assert c.CreateChatRequest(kind="protocol").kind == c.SessionKind.protocol
    assert c.CreateChatRequest(kind="budget", protocol_id=pid).protocol_id == pid
    assert c.CreateChatRequest(kind="protocol_edit", protocol_id=pid.upper()).protocol_id == pid  # normalised
    with pytest.raises(ValidationError):
        c.CreateChatRequest(kind="protocol_edit")
    with pytest.raises(ValidationError):
        c.CreateChatRequest(kind="protocol", protocol_id=pid)
    with pytest.raises(ValidationError):
        c.CreateChatRequest(kind="protocol_edit", protocol_id="not-a-uuid")
    with pytest.raises(ValidationError):
        c.CreateChatRequest(kind="wizard")


def test_patch_chat_rules():
    assert c.PatchChatRequest(title="  New name ").title == "New name"
    assert c.PatchChatRequest(status="archived").status == "archived"
    assert c.PatchChatRequest(title="x" * 200).title == "x" * 200
    with pytest.raises(ValidationError):
        c.PatchChatRequest(title="x" * 201)
    with pytest.raises(ValidationError):
        c.PatchChatRequest()
    with pytest.raises(ValidationError):
        c.PatchChatRequest(title="   ")  # blank counts as not provided
    with pytest.raises(ValidationError):
        c.PatchChatRequest(status="completed")  # server-controlled


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

VALID_PAYLOADS: dict[str, dict] = {
    "select_drug": {"drug_id": "3f2b8c1e-1111-4222-8333-444455556666"},
    "add_drug": {"inn": "Adalimumab"},
    "set_study_type": {"study_type": "biosimilar_pk"},
    "set_guideline": {"guideline_version": "EMA-BE-2010"},
    "update_population": {"patch": {"age_min": 18, "age_max": 45}},
    "toggle_exclusion": {"text": "Known hypersensitivity", "on": True},
    "set_design_decision": {"key": "population", "value": "patients"},
    "set_indication": {"text": "rheumatoid arthritis"},
    "confirm_design": {},
    "generate_protocol": {},
    "set_budget_fields": {"patch": {"n_sites": 40}},
    "link_protocol": {"protocol_id": "3f2b8c1e-1111-4222-8333-444455556666"},
    "create_budget": {},
    "confirm_edit": {"proposal_id": "3f2b8c1e-1111-4222-8333-444455556666"},
    "apply_edit": {"proposal_id": "3f2b8c1e-1111-4222-8333-444455556666"},
    "discard_edit": {"proposal_id": "3f2b8c1e-1111-4222-8333-444455556666"},
}

INVALID_PAYLOADS: list[tuple[str, dict]] = [
    ("select_drug", {}),
    ("select_drug", {"drug_id": "adalimumab"}),
    ("select_drug", {"drug_id": "3f2b8c1e-1111-4222-8333-444455556666", "extra": 1}),
    ("add_drug", {"inn": "a"}),
    ("add_drug", {"inn": "drug; DROP TABLE"}),
    ("add_drug", {"inn": "x" * 65}),
    ("add_drug", {"inn": "1abc"}),
    ("set_study_type", {"study_type": "phase3"}),
    ("set_study_type", {}),
    ("set_guideline", {"guideline_version": ""}),
    ("update_population", {"patch": {}}),
    ("update_population", {}),
    ("toggle_exclusion", {"text": "", "on": True}),
    ("toggle_exclusion", {"text": "x" * 301, "on": True}),
    ("toggle_exclusion", {"text": "ok"}),
    ("set_design_decision", {"key": "population"}),
    ("set_design_decision", {"key": "", "value": "x"}),
    ("set_indication", {}),
    ("set_indication", {"text": "x" * 201}),
    ("confirm_design", {"unexpected": True}),
    ("generate_protocol", {"confirm": True}),
    ("set_budget_fields", {"patch": {}}),
    ("link_protocol", {}),
    ("link_protocol", {"protocol_id": "nope"}),
    ("confirm_edit", {}),
    ("apply_edit", {"proposal_id": 7}),
    ("discard_edit", {"proposal_id": ""}),
]


def test_every_action_has_a_payload_model_and_session_kinds():
    assert set(c.ACTION_PAYLOAD_MODELS) == set(c.ActionType)
    assert set(c.ACTION_SESSION_KINDS) == set(c.ActionType)
    assert set(VALID_PAYLOADS) == {a.value for a in c.ActionType}
    assert c.ACTIONS_REQUIRING_LICENSE == {c.ActionType.generate_protocol, c.ActionType.confirm_edit}
    for kinds in c.ACTION_SESSION_KINDS.values():
        assert len(kinds) == 1


@pytest.mark.parametrize("action", list(VALID_PAYLOADS))
def test_action_request_accepts_valid_payload(action):
    req = c.ActionRequest(card_id=_uid(), action=action, payload=VALID_PAYLOADS[action], spec_version=3)
    typed = req.parsed_payload()
    assert isinstance(typed, c.ACTION_PAYLOAD_MODELS[c.ActionType(action)])


@pytest.mark.parametrize("action,payload", INVALID_PAYLOADS, ids=[f"{a}-{i}" for i, (a, _) in enumerate(INVALID_PAYLOADS)])
def test_action_request_rejects_invalid_payload(action, payload):
    with pytest.raises(ValidationError) as err:
        c.ActionRequest(card_id=_uid(), action=action, payload=payload, spec_version=1)
    assert action in str(err.value)


def test_action_payload_values_are_normalised():
    inn = c.ActionRequest(card_id=_uid(), action="add_drug", payload={"inn": "  Adalimumab  "}, spec_version=0).parsed_payload()
    assert inn.inn == "adalimumab"
    cleared = c.ActionRequest(card_id=_uid(), action="set_indication", payload={"text": "   "}, spec_version=0).parsed_payload()
    assert cleared.text is None
    none_ok = c.ActionRequest(card_id=_uid(), action="set_indication", payload={"text": None}, spec_version=0).parsed_payload()
    assert none_ok.text is None
    unlink = c.ActionRequest(card_id=_uid(), action="link_protocol", payload={"protocol_id": None}, spec_version=0).parsed_payload()
    assert unlink.protocol_id is None


def test_action_request_envelope_validation():
    ok = dict(card_id=_uid(), action="confirm_design", payload={}, spec_version=0)
    assert c.ActionRequest(**ok).payload == {}
    assert c.ActionRequest(card_id=ok["card_id"], action="confirm_design", spec_version=0).payload == {}
    with pytest.raises(ValidationError):
        c.ActionRequest(**{**ok, "card_id": "not-a-uuid"})
    with pytest.raises(ValidationError):
        c.ActionRequest(**{**ok, "action": "delete_everything"})
    with pytest.raises(ValidationError):
        c.ActionRequest(**{**ok, "spec_version": -1})
    with pytest.raises(ValidationError):
        c.ActionRequest(card_id=ok["card_id"], action="confirm_design", payload={})  # spec_version required
    with pytest.raises(ValidationError):
        c.ActionRequest(**ok, surprise=True)


def _spec(version=4, ready=False) -> c.SpecSummary:
    return c.SpecSummary(
        kind="protocol",
        spec_version=version,
        ready=ready,
        missing=["drug.inn", "study_type"],
        fields=[
            c.SpecField(path="drug.inn", label="Drug", status="empty"),
            c.SpecField(path="guideline_version", label="Guideline", display_value="FDA-BE-2014", status="default", source="default"),
            c.SpecField(path="population.age_min", label="Minimum age", display_value="18", status="confirmed", source="user_click"),
            c.SpecField(path="cv_intra", label="Intra-subject CV", display_value="22%", status="engine", source="engine"),
            c.SpecField(path="population.sex", label="Sex", display_value="Both", status="proposed", source="user_text"),
        ],
    )


def test_action_response_round_trips_and_checks_version_agreement():
    resp = c.ActionResponse(
        spec_version=4,
        spec=_spec(4),
        cards=[_card()],
        guide=c.Guide(text="Pick a study type.", chips=["Biosimilar PK", "Bioequivalence"]),
    )
    wire = resp.model_dump_json()
    assert c.ActionResponse.model_validate_json(wire) == resp
    parsed = json.loads(wire)
    assert set(parsed) == {"spec_version", "spec", "cards", "guide"}
    assert parsed["guide"] == {"text": "Pick a study type.", "chips": ["Biosimilar PK", "Bioequivalence"]}
    with pytest.raises(ValidationError):
        c.ActionResponse(spec_version=5, spec=_spec(4), guide=c.Guide(text="x"))


# ---------------------------------------------------------------------------
# Spec summary
# ---------------------------------------------------------------------------


def test_spec_summary_round_trips_and_allows_empty_value_and_source():
    spec = _spec()
    again = c.SpecSummary.model_validate_json(spec.model_dump_json())
    assert again == spec
    empty = json.loads(spec.model_dump_json())["fields"][0]
    assert empty == {"path": "drug.inn", "label": "Drug", "display_value": None, "status": "empty", "source": None}


def test_spec_field_status_and_source_are_closed_sets():
    with pytest.raises(ValidationError):
        c.SpecField(path="p", label="L", status="guessed")
    with pytest.raises(ValidationError):
        c.SpecField(path="p", label="L", status="engine", source="llm")


# ---------------------------------------------------------------------------
# Session / message / detail models
# ---------------------------------------------------------------------------


def _orm_session(**over):
    ts = datetime(2026, 10, 23, 9, 30)  # naive, as some drivers return
    base = dict(
        id=uuid.uuid4(), kind="protocol", status="active", title=None, spec_version=3,
        protocol_id=None, budget_id=None, created_at=ts, updated_at=ts, last_activity_at=ts,
        org_id="org-1", created_by=uuid.uuid4(), turn_lock_id=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_session_models_read_from_orm_attributes():
    row = _orm_session(protocol_id=uuid.uuid4())
    read = c.ChatSessionRead.model_validate(row)
    assert read.id == str(row.id) and read.protocol_id == str(row.protocol_id)
    assert read.kind == c.SessionKind.protocol
    wire = json.loads(read.model_dump_json())
    assert wire["created_at"].endswith("Z")  # naive input is treated as UTC
    assert "org_id" not in wire and "created_by" not in wire and "turn_lock_id" not in wire

    item = c.ChatSessionListItem.model_validate(row)
    assert set(json.loads(item.model_dump_json())) == {
        "id", "kind", "status", "title", "protocol_id", "budget_id", "created_at", "last_activity_at",
    }


def test_datetimes_are_normalised_to_utc():
    row = _orm_session(last_activity_at=datetime.fromisoformat("2026-10-23T12:00:00+02:00"))
    wire = json.loads(c.ChatSessionRead.model_validate(row).model_dump_json())
    assert wire["last_activity_at"] == "2026-10-23T10:00:00Z"


def test_session_detail_round_trips():
    msg = c.ChatMessageDisplay(
        id=uuid.uuid4(), seq=1, role="assistant", text="Here is the plan.", card_ids=[uuid.uuid4()],
        flags=c.MessageFlags(unverified_numbers=["42"]), created_at=datetime(2026, 10, 23, 9, 31),
    )
    detail = c.ChatSessionDetail(
        session=c.ChatSessionRead.model_validate(_orm_session()),
        messages=[msg],
        cards=[_card()],
        spec=_spec(),
        awaiting_reply=True,
    )
    wire = detail.model_dump_json()
    assert c.ChatSessionDetail.model_validate_json(wire) == detail
    parsed = json.loads(wire)
    assert set(parsed) == {"session", "messages", "cards", "spec", "awaiting_reply"}
    assert set(parsed["messages"][0]) == {"id", "seq", "role", "text", "card_ids", "flags", "created_at"}
    assert parsed["messages"][0]["flags"] == {"unverified_numbers": ["42"]}


def test_message_display_defaults_and_unknown_flag_keys_are_ignored():
    msg = c.ChatMessageDisplay.model_validate(
        dict(id=_uid(), seq=0, role="user", text="hi", created_at="2026-10-23T09:31:00Z", flags={"unverified_numbers": [], "future": 1})
    )
    assert msg.card_ids == [] and msg.flags.unverified_numbers == []
    with pytest.raises(ValidationError):
        c.ChatMessageDisplay(id=_uid(), seq=0, role="narrator", text="hi", created_at="2026-10-23T09:31:00Z")


def test_proposal_read_round_trips():
    now = datetime(2026, 10, 23, 9, 30, tzinfo=timezone.utc)
    prop = c.ProposalRead(
        id=uuid.uuid4(), protocol_id=uuid.uuid4(), session_id=uuid.uuid4(), kind="engine", status="ready",
        base_version=3, request={"changes": [{"field": "cv_intra", "value": 30}]},
        diff=[c.SectionDiffItem(section_name="synopsis", display_name="Synopsis", status="modified", diff_lines=["-a", "+b"], word_count_delta=2)],
        qc={"findings": []}, facts_before={"n": 24}, facts_after={"n": 32},
        created_at=now, updated_at=now,
    )
    assert c.ProposalRead.model_validate_json(prop.model_dump_json()) == prop
    with pytest.raises(ValidationError):
        c.ProposalRead(id=_uid(), protocol_id=_uid(), kind="engine", status="exploded", base_version=1, created_at=now, updated_at=now)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_every_http_error_code_has_a_status():
    sse_only = {c.ChatErrorCode.stream_interrupted}
    assert set(c.ERROR_HTTP_STATUS) == set(c.ChatErrorCode) - sse_only
    assert c.ERROR_HTTP_STATUS[c.ChatErrorCode.busy] == 409
    assert c.ERROR_HTTP_STATUS[c.ChatErrorCode.stale_spec] == 409
    assert c.ERROR_HTTP_STATUS[c.ChatErrorCode.license_required] == 402
    assert c.ERROR_HTTP_STATUS[c.ChatErrorCode.rate_limited] == 429
    assert c.ERROR_HTTP_STATUS[c.ChatErrorCode.daily_cap] == 429


def test_error_detail_is_a_plain_json_dict():
    detail = c.error_detail(c.ChatErrorCode.busy, "Another reply is in progress.")
    assert detail == {"code": "busy", "message": "Another reply is in progress."}
    assert c.error_detail("custom", "m") == {"code": "custom", "message": "m"}


# ---------------------------------------------------------------------------
# Purity: importing the contract must not drag in the app
# ---------------------------------------------------------------------------


def test_contract_import_is_pure():
    code = (
        "import sys; import services.chat.contract; "
        "bad = sorted(m for m in ('sqlalchemy', 'fastapi', 'anthropic', 'database', 'models', 'main', 'asyncpg') if m in sys.modules); "
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND_DIR, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"contract pulled in: {out.stdout.strip()}"


# ---------------------------------------------------------------------------
# TypeScript mirror drift guard
# ---------------------------------------------------------------------------

TS_UNION_MIRRORS = [
    ("SessionKind", c.SessionKind),
    ("SessionStatus", c.SessionStatus),
    ("MessageRole", c.MessageRole),
    ("CardType", c.CardType),
    ("CardStatus", c.CardStatus),
    ("ActionType", c.ActionType),
    ("FieldStatus", c.FieldStatus),
    ("FieldSource", c.FieldSource),
    ("ProposalKind", c.ProposalKind),
    ("ProposalStatus", c.ProposalStatus),
    ("ChatErrorCode", c.ChatErrorCode),
    ("SseEventName", c.SseEventName),
]


def _ts_source() -> str:
    if not TYPES_TS.exists():
        pytest.skip("frontend/src/chat/types.ts not present (backend-only checkout)")
    return TYPES_TS.read_text(encoding="utf-8")


def _ts_union(src: str, name: str) -> list[str]:
    m = re.search(rf"export type {name} =\s*((?:\|\s*'[a-z_]+'\s*)+)", src)
    assert m, f"types.ts has no multi-line string-literal union named {name}"
    return re.findall(r"'([a-z_]+)'", m.group(1))


@pytest.mark.parametrize("ts_name,enum_cls", TS_UNION_MIRRORS, ids=[n for n, _ in TS_UNION_MIRRORS])
def test_typescript_union_matches_python_enum(ts_name, enum_cls):
    assert _ts_union(_ts_source(), ts_name) == [m.value for m in enum_cls]


def test_typescript_small_literal_unions_match():
    from typing import get_args

    src = _ts_source()
    assert _ts_union(src, "CardActionVariant") == list(get_args(c.CardActionVariant))
    assert _ts_union(src, "NoticeLevel") == list(get_args(c.NoticeLevel))


def test_typescript_limits_match():
    src = _ts_source()
    for name in (
        "MAX_TURN_TEXT", "MAX_INITIAL_MESSAGE", "MAX_TITLE", "MAX_CLIENT_TURN_ID", "MAX_INDICATION_LENGTH",
        "MAX_EXCLUSION_TEXT", "DEFAULT_LIST_LIMIT", "MAX_LIST_LIMIT",
    ):
        m = re.search(rf"export const {name} = (\d+)", src)
        assert m, f"types.ts is missing {name}"
        assert int(m.group(1)) == getattr(c, name), name
    m = re.search(r"export const DEFAULT_GUIDELINE_VERSION = '([^']+)'", src)
    assert m and m.group(1) == c.DEFAULT_GUIDELINE_VERSION


def test_typescript_action_session_kinds_match():
    src = _ts_source()
    block = re.search(r"export const ACTION_SESSION_KINDS[^{]*\{(.*?)\n\}", src, re.S)
    assert block, "types.ts is missing ACTION_SESSION_KINDS"
    ts = {
        m.group(1): re.findall(r"'([a-z_]+)'", m.group(2))
        for m in re.finditer(r"(\w+): \[([^\]]*)\]", block.group(1))
    }
    py = {a.value: sorted(k.value for k in kinds) for a, kinds in c.ACTION_SESSION_KINDS.items()}
    assert {k: sorted(v) for k, v in ts.items()} == py


def test_typescript_action_payload_map_covers_every_action():
    src = _ts_source()
    block = re.search(r"export interface ActionPayloadMap \{(.*?)\n\}", src, re.S)
    assert block, "types.ts is missing ActionPayloadMap"
    keys = re.findall(r"^\s{2}(\w+):", block.group(1), re.M)
    assert keys == [a.value for a in c.ActionType]
