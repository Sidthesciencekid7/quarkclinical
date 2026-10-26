"""Parallel protocol writing over one cached prompt.

A real generation (2026-10-25) spent ~17 of its ~24 minutes in ONE request
writing all ~50k output tokens. The sections are now written in contiguous
groups at the same time; every request shares a byte-identical prefix (tool,
system, shared text) marked for prompt caching, and the rest are launched only
once the first response streams, so they read the cache instead of paying for
the ~50k-token context again. A cut-off group must still fail the whole
protocol loudly (see test_generation_truncation.py).
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from services import three_stage_pipeline as tsp

ALL = [
    "title", "synopsis", "introduction", "study_rationale", "objectives", "endpoints",
    "study_design", "population", "treatment", "study_assessments", "safety_monitoring",
    "data_handling", "study_monitoring", "statistical_analysis_plan", "ethics",
    "administrative", "references",
]
CONTEXT = {"guideline_version": "FDA-BE-2014", "drug_inn": "metformin"}


def _requested(kwargs) -> list[str]:
    text = kwargs["messages"][0]["content"][1]["text"]
    m = re.search(r"Write ONLY these sections now: ([^.]+)\.", text)
    if m:
        return [s.strip() for s in m.group(1).split(",")]
    m = re.search(r"listed above \(([^)]+)\)", text)
    return [s.strip() for s in m.group(1).split(",")]


class _Stream:
    def __init__(self, owner, kwargs, idx):
        self.owner, self.kwargs, self.idx = owner, kwargs, idx

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        await asyncio.sleep(0.01)
        self.owner.events.append(("first_event", self.idx))
        yield SimpleNamespace(type="message_start")
        await asyncio.sleep(0.01)
        yield SimpleNamespace(type="message_stop")

    async def get_final_message(self):
        group = _requested(self.kwargs)
        attempt = self.owner.attempts.setdefault(tuple(group), 0)
        self.owner.attempts[tuple(group)] = attempt + 1
        truncate = self.owner.truncate.get(tuple(group), 0) > attempt
        values = {} if truncate else {n: f"<p>{n}</p>" for n in group}
        first_write = self.idx == 0
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name="write_protocol", input=values)],
            stop_reason="max_tokens" if truncate else "end_turn",
            usage=SimpleNamespace(
                input_tokens=500, output_tokens=1000,
                cache_creation_input_tokens=50000 if first_write else 0,
                cache_read_input_tokens=0 if first_write else 50000,
            ),
        )


class _Messages:
    def __init__(self, owner):
        self.owner = owner

    def stream(self, **kwargs):
        idx = len(self.owner.calls)
        self.owner.calls.append(kwargs)
        self.owner.events.append(("start", idx))
        return _Stream(self.owner, kwargs, idx)


class _Client:
    def __init__(self, truncate=None):
        self.calls: list[dict] = []
        self.events: list[tuple[str, int]] = []
        self.attempts: dict[tuple, int] = {}
        self.truncate = truncate or {}  # group tuple -> how many attempts come back cut off
        self.messages = _Messages(self)


@pytest.fixture(autouse=True)
def _six_way(monkeypatch):
    monkeypatch.setenv("GENERATION_PARALLELISM", "6")


# --- grouping ----------------------------------------------------------------


def test_sections_split_into_contiguous_balanced_groups():
    groups = tsp._partition_sections(ALL, 6)
    assert 1 < len(groups) <= 6
    assert [n for g in groups for n in g] == ALL, "every section exactly once, in document order"
    heaviest = max(sum(tsp._SECTION_WEIGHTS[n] for n in g) for g in groups)
    # One request used to carry all ~197k characters; no group may come close.
    assert heaviest <= 45000
    assert any({"objectives", "endpoints"} <= set(g) for g in groups), "related sections stay together"


def test_one_group_when_parallelism_is_one(monkeypatch):
    monkeypatch.setenv("GENERATION_PARALLELISM", "1")
    client = _Client()
    sections, log = asyncio.run(tsp._stage2_generate_parallel(client, "BE", CONTEXT, ALL))
    assert len(client.calls) == 1 and log["parallel_groups"] == 1
    assert set(sections) == set(ALL)


# --- the shared, cached prompt ---------------------------------------------


def test_every_request_shares_one_cached_prefix_and_names_its_own_sections():
    client = _Client()
    sections, log = asyncio.run(tsp._stage2_generate_parallel(client, "BE", CONTEXT, ALL))
    assert log["status"] == "success"
    assert set(sections) == set(ALL) and all(sections.values())
    assert len(client.calls) == log["parallel_groups"] > 1

    first = client.calls[0]
    for call in client.calls:
        assert call["tools"] == first["tools"], "a different tool would break the cache"
        assert call["system"] == first["system"]
        assert call["messages"][0]["content"][0] == first["messages"][0]["content"][0]
        assert call["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert call["max_tokens"] == tsp.MAX_PROTOCOL_OUTPUT_TOKENS
        assert call["tool_choice"] == {"type": "tool", "name": "write_protocol"}
    requested = [n for call in client.calls for n in _requested(call)]
    assert sorted(requested) == sorted(ALL), "each section requested exactly once"
    assert first["tools"][0]["input_schema"]["required"] == []


def test_the_others_start_only_after_the_first_response_streams():
    client = _Client()
    asyncio.run(tsp._stage2_generate_parallel(client, "BE", CONTEXT, ALL))
    first_event = client.events.index(("first_event", 0))
    second_start = client.events.index(("start", 1))
    assert first_event < second_start, client.events


def test_log_sums_usage_including_cache_reads():
    client = _Client()
    _, log = asyncio.run(tsp._stage2_generate_parallel(client, "BE", CONTEXT, ALL))
    n = log["parallel_groups"]
    assert log["output_tokens"] == 1000 * n
    assert log["cache_creation_input_tokens"] == 50000
    assert log["cache_read_input_tokens"] == 50000 * (n - 1)


# --- failures stay loud ------------------------------------------------------


def test_a_cut_off_group_is_retried_once_and_then_succeeds():
    groups = tsp._partition_sections(ALL, 6)
    client = _Client(truncate={tuple(groups[2]): 1})
    sections, log = asyncio.run(tsp._stage2_generate_parallel(client, "BE", CONTEXT, ALL))
    assert log["status"] == "success" and log["groups_retried"] == 1
    assert set(sections) == set(ALL)


def test_a_group_cut_off_twice_fails_the_whole_protocol_and_names_the_missing_sections():
    groups = tsp._partition_sections(ALL, 6)
    client = _Client(truncate={tuple(groups[2]): 2})
    sections, log = asyncio.run(tsp._stage2_generate_parallel(client, "BE", CONTEXT, ALL))
    assert sections == {}, "a partial protocol must never be handed on"
    assert log["status"] == "failed"
    assert f"{len(ALL) - len(groups[2])} of {len(ALL)}" in log["error"]
    for name in groups[2]:
        assert name in log["error"]


# --- remediation reuses the same cache ---------------------------------------


def test_remediation_writes_flagged_sections_in_parallel_over_the_same_prefix(monkeypatch):
    client = _Client()
    asyncio.run(tsp._stage2_generate_parallel(client, "BE", CONTEXT, ALL))
    generation_prefix = client.calls[0]["messages"][0]["content"][0]
    generation_tools = client.calls[0]["tools"]

    report = {"findings": [
        {"status": "fail", "section": "population", "title": "A", "detail": "fix a"},
        {"status": "warning", "section": "safety_monitoring", "title": "B", "detail": "fix b"},
    ]}
    monkeypatch.setattr(tsp, "run_qc", lambda **kw: SimpleNamespace(to_dict=lambda: report))
    client.calls.clear()
    base = {n: f"<p>{n}</p>" for n in ALL}
    sections, qc, log = asyncio.run(tsp._qc_remediate(client, "BE", CONTEXT, base, ALL))

    assert log["status"] == "success" and log["stage"] == "stage2_remediate"
    assert sorted(_requested(c)[0] for c in client.calls) == ["population", "safety_monitoring"]
    for call in client.calls:
        assert call["messages"][0]["content"][0] == generation_prefix
        assert call["tools"] == generation_tools
    assert "fix a" in client.calls[[_requested(c)[0] for c in client.calls].index("population")]["messages"][0]["content"][1]["text"]
    assert qc["sections_remediated"] == ["population", "safety_monitoring"]
