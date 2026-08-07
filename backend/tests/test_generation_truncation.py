"""Regression tests for silently-truncated protocol generation.

All 17 sections are produced by ONE Sonnet call, as fields of a single
write_protocol tool call. When that response hits the max_tokens ceiling the
tool call is cut off mid-structure and the missing fields come back as empty
strings -- leaving a dict that is still fully-keyed and truthy.

That is exactly how a real run produced 10 of 17 sections, was logged
"success", persisted, and shown to the user as a finished protocol with no
error anywhere in the logs or the API response. For a regulated document a
silently-incomplete output is the worst available failure mode, so these tests
pin the detection rather than the token number.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from services.three_stage_pipeline import (
    MAX_PROTOCOL_OUTPUT_TOKENS,
    _stage2_generate_all_sections,
)

SECTIONS = ["title", "synopsis", "introduction", "objectives", "endpoints"]


class _FakeStream:
    def __init__(self, message):
        self._message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self._message


class _FakeMessages:
    """Captures the request kwargs and replays a canned message."""

    def __init__(self, message):
        self._message = message
        self.kwargs: dict = {}

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return _FakeStream(self._message)


class _FakeClient:
    def __init__(self, message):
        self.messages = _FakeMessages(message)


def _message(section_values: dict[str, str], stop_reason: str):
    """Build a message whose write_protocol tool call carries section_values."""
    block = SimpleNamespace(
        type="tool_use",
        name="write_protocol",
        input=dict(section_values),
    )
    return SimpleNamespace(
        content=[block],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=20000),
    )


def _run(message):
    client = _FakeClient(message)
    sections, log = asyncio.run(
        _stage2_generate_all_sections(client, "BE", {"guideline_version": "FDA-BE-2014"}, SECTIONS)
    )
    return sections, log, client


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


def test_truncated_generation_is_not_reported_as_success():
    """stop_reason == max_tokens must fail, even though the dict is fully keyed."""
    partial = {"title": "T", "synopsis": "S"}  # 2 of 5; rest absent -> empty
    sections, log, _ = _run(_message(partial, "max_tokens"))
    assert log["status"] == "failed", "a cut-off generation must never log success"
    assert sections == {}, "partial sections must not be handed on as a protocol"


def test_failure_message_names_what_is_missing():
    """The operator has to be able to see WHICH sections were lost."""
    sections, log, _ = _run(_message({"title": "T", "synopsis": "S"}, "max_tokens"))
    err = log.get("error") or ""
    assert "2 of 5" in err, f"should report the section count, got: {err}"
    for missing in ("introduction", "objectives", "endpoints"):
        assert missing in err, f"{missing} should be named as missing, got: {err}"
    assert str(MAX_PROTOCOL_OUTPUT_TOKENS) in err, "should cite the ceiling that was hit"


def test_incomplete_sections_fail_even_without_a_max_tokens_stop_reason():
    """Belt and braces: an empty section is a broken protocol whatever the API said."""
    sections, log, _ = _run(_message({"title": "T", "synopsis": ""}, "end_turn"))
    assert log["status"] == "failed"
    assert sections == {}


# ---------------------------------------------------------------------------
# The happy path still works
# ---------------------------------------------------------------------------


def test_complete_generation_succeeds():
    full = {name: f"<p>{name}</p>" for name in SECTIONS}
    sections, log, _ = _run(_message(full, "end_turn"))
    assert log["status"] == "success"
    assert set(sections) == set(SECTIONS)
    assert all(sections.values())


# ---------------------------------------------------------------------------
# The ceiling itself
# ---------------------------------------------------------------------------


def test_ceiling_is_actually_sent_to_the_api():
    """A constant nobody passes is not a fix."""
    full = {name: "x" for name in SECTIONS}
    _, _, client = _run(_message(full, "end_turn"))
    assert client.messages.kwargs.get("max_tokens") == MAX_PROTOCOL_OUTPUT_TOKENS


def test_ceiling_leaves_real_headroom_over_the_17_section_document():
    """20000 truncated a 17-section protocol at ~10 sections. Guard the regression:
    the ceiling must stay comfortably above that, and inside the model's 128k cap."""
    assert MAX_PROTOCOL_OUTPUT_TOKENS >= 48000, "too close to the size that truncated"
    assert MAX_PROTOCOL_OUTPUT_TOKENS <= 128000, "exceeds the model's output ceiling"


def test_generation_streams_because_a_large_ceiling_requires_it():
    """A non-streaming request at this max_tokens would hit the SDK HTTP timeout."""
    full = {name: "x" for name in SECTIONS}
    _, _, client = _run(_message(full, "end_turn"))
    assert client.messages.kwargs, "expected the call to go through messages.stream()"
