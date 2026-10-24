"""Pure tests for the price table + cost math (services/llm/pricing.py) and the
usage-coercion/never-raises behaviour of services/llm/usage.record_usage.

No real database: record_usage's session factory is faked at the
`database.AsyncSessionLocal` attribute (the same attribute the DB test fixture
swaps), which also proves it is looked up at call time.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

import database
from models import LlmFeature
from services.llm import pricing, usage
from services.llm.pricing import PRICES, coerce_usage, cost_usd, normalize_model_id


# ---------------------------------------------------------------------------
# Price table integrity
# ---------------------------------------------------------------------------


def test_table_covers_the_models_the_app_uses():
    for model in (
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
    ):
        assert model in PRICES


def test_headline_prices_match_the_plan():
    r = PRICES["claude-opus-5-5"].rates
    assert (r.input, r.output, r.cache_write_5m, r.cache_read) == (
        Decimal("4.00"), Decimal("20.00"), Decimal("5.00"), Decimal("0.20"),
    )
    r = PRICES["claude-sonnet-5-5"].rates
    assert (r.input, r.output, r.cache_write_5m, r.cache_read) == (
        Decimal("2.00"), Decimal("10.00"), Decimal("2.50"), Decimal("0.20"),
    )
    r = PRICES["claude-haiku-5-5"].rates
    assert (r.input, r.output) == (Decimal("0.10"), Decimal("0.50"))
    r = PRICES["claude-sonnet-4-6"].rates
    assert (r.input, r.output) == (Decimal("3.00"), Decimal("15.00"))
    r = PRICES["claude-opus-4-6"].rates
    assert (r.input, r.output) == (Decimal("5.00"), Decimal("25.00"))


def test_cache_multipliers_are_consistent_for_every_rate_card():
    """5-minute write = 1.25x input, 1-hour write = 2x input; cache read = 0.1x
    input everywhere except Opus 5.5 (0.05x)."""
    for model, price in PRICES.items():
        cards = [price.rates] + ([price.long_prompt_rates] if price.long_prompt_rates else [])
        for r in cards:
            assert r.cache_write_5m == r.input * Decimal("1.25"), model
            assert r.cache_write_1h == r.input * Decimal("2"), model
            expected_read = Decimal("0.05") if model == "claude-opus-5-5" else Decimal("0.1")
            assert r.cache_read == r.input * expected_read, model


# ---------------------------------------------------------------------------
# cost_usd
# ---------------------------------------------------------------------------


def test_zero_usage_costs_zero():
    assert cost_usd("claude-opus-5-5", {}) == Decimal("0")
    assert cost_usd("claude-opus-5-5", None) == Decimal("0")


def test_plain_input_and_output():
    # 1,000,000 in + 1,000,000 out on Opus 5.5 = $4 + $20
    assert cost_usd(
        "claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    ) == Decimal("24.000000")


def test_cache_read_and_write_priced_separately():
    usage_ = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,  # 5-minute write
        "cache_read_input_tokens": 1_000_000,
    }
    assert cost_usd("claude-opus-5-5", usage_) == Decimal("5.200000")  # 5.00 + 0.20
    assert cost_usd("claude-sonnet-5-5", usage_) == Decimal("2.700000")  # 2.50 + 0.20
    assert cost_usd("claude-sonnet-4-6", usage_) == Decimal("4.050000")  # 3.75 + 0.30
    assert cost_usd("claude-opus-4-6", usage_) == Decimal("6.750000")  # 6.25 + 0.50


def test_plan_example_typical_chat_call():
    """Plan section G: ~9K cache-read, ~1.2K cache-write, ~350 output on Opus 5.5
    is roughly $0.015 per call."""
    cost = cost_usd(
        "claude-opus-5-5",
        {
            "input_tokens": 0,
            "cache_read_input_tokens": 9_000,
            "cache_creation_input_tokens": 1_200,
            "output_tokens": 350,
        },
    )
    # 9000*0.20 + 1200*5.00 + 350*20.00 = 1800 + 6000 + 7000 = 14,800 / 1e6
    assert cost == Decimal("0.014800")
    assert Decimal("0.014") < cost < Decimal("0.016")


def test_result_is_a_decimal_rounded_to_six_places():
    cost = cost_usd("claude-sonnet-4-6", {"input_tokens": 1, "output_tokens": 1})
    # (1*3 + 1*15) / 1e6 = 0.000018
    assert isinstance(cost, Decimal)
    assert cost == Decimal("0.000018")
    # sub-quantum amounts round half up
    assert cost_usd("claude-haiku-5-5", {"input_tokens": 1}) == Decimal("0.000000")  # 1e-7
    assert cost_usd("claude-haiku-5-5", {"input_tokens": 5}) == Decimal("0.000001")  # 5e-7


def test_sdk_style_usage_object_is_accepted():
    sdk_usage = SimpleNamespace(
        input_tokens=500,
        output_tokens=200,
        cache_creation_input_tokens=None,  # the SDK reports None when absent
        cache_read_input_tokens=2000,
    )
    # (500*4 + 200*20 + 2000*0.20) / 1e6 = (2000 + 4000 + 400) / 1e6
    assert cost_usd("claude-opus-5-5", sdk_usage) == Decimal("0.006400")


def test_one_hour_cache_writes_are_billed_at_the_one_hour_rate():
    usage_ = {
        "cache_creation_input_tokens": 1_000_000,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 400_000,
            "ephemeral_1h_input_tokens": 600_000,
        },
    }
    # Opus 5.5: 0.4M * $5 + 0.6M * $8 = 2.00 + 4.80
    assert cost_usd("claude-opus-5-5", usage_) == Decimal("6.800000")


def test_haiku_long_prompt_tier():
    # A prompt exactly at the 100K threshold is still the standard card:
    # 100,000 in @ $0.10 + 1,000,000 out @ $0.50 = 0.01 + 0.50
    assert cost_usd(
        "claude-haiku-5-5", {"input_tokens": 100_000, "output_tokens": 1_000_000}
    ) == Decimal("0.510000")
    # One token over: input @ $0.50 and output @ $2.50.
    # 100,001 * 0.50 + 1,000,000 * 2.50 = 2,550,000.5 / 1e6 -> 2.5500005 -> rounds half up
    assert cost_usd(
        "claude-haiku-5-5", {"input_tokens": 100_001, "output_tokens": 1_000_000}
    ) == Decimal("2.550001")
    # Cache tokens count toward the prompt length: 1,000 + 100,000 = 101,000 > 100K,
    # so the long card applies: 1,000 * 0.50 + 100,000 * 0.05 = 5,500 / 1e6
    assert cost_usd(
        "claude-haiku-5-5", {"input_tokens": 1_000, "cache_read_input_tokens": 100_000}
    ) == Decimal("0.005500")


def test_negative_or_junk_token_counts_are_treated_as_zero():
    usage_ = {"input_tokens": -5, "output_tokens": "lots", "cache_read_input_tokens": True}
    assert coerce_usage(usage_).input_tokens == 0
    assert coerce_usage(usage_).output_tokens == 0
    assert coerce_usage(usage_).cache_read_input_tokens == 0
    assert cost_usd("claude-opus-5-5", usage_) == Decimal("0")


# ---------------------------------------------------------------------------
# Unknown / differently spelt models
# ---------------------------------------------------------------------------


def test_unknown_model_costs_zero_and_does_not_raise(caplog):
    pricing._warned_models.discard("claude-future-9")
    with caplog.at_level(logging.WARNING, logger="services.llm.pricing"):
        cost = cost_usd("claude-future-9", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
        cost_again = cost_usd("claude-future-9", {"input_tokens": 5})
    assert cost == Decimal("0.000000")
    assert cost_again == Decimal("0.000000")
    # warned once, not once per call
    assert sum("claude-future-9" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("model", [None, "", "   "])
def test_missing_model_costs_zero(model):
    assert cost_usd(model, {"input_tokens": 100}) == Decimal("0")


@pytest.mark.parametrize(
    "spelling",
    [
        "claude-sonnet-4-6",
        "CLAUDE-SONNET-4-6",
        "anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-sonnet-4-6-20260101-v1:0",
        "global.anthropic.claude-sonnet-4-6",
    ],
)
def test_bedrock_and_versioned_spellings_normalise(spelling):
    assert normalize_model_id(spelling) == "claude-sonnet-4-6"
    assert cost_usd(spelling, {"input_tokens": 1_000_000}) == Decimal("3.000000")


# ---------------------------------------------------------------------------
# record_usage: builds the right row, never raises
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, store: list, *, fail_commit: bool = False):
        self._store = store
        self._fail_commit = fail_commit

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, row):
        self._store.append(row)

    async def commit(self):
        if self._fail_commit:
            raise RuntimeError("database is down")


@pytest.fixture
def fake_db(monkeypatch):
    rows: list = []
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: _FakeSession(rows))
    return rows


async def test_record_usage_writes_one_priced_row(fake_db):
    user_id = uuid.uuid4()
    await usage.record_usage(
        org_id="org-a",
        user_id=user_id,
        feature="chat",
        resource_type="chat_session",
        resource_id=uuid.uuid4(),
        provider="anthropic",
        model="claude-opus-5-5",
        usage={
            "input_tokens": 100,
            "output_tokens": 350,
            "cache_creation_input_tokens": 1_200,
            "cache_read_input_tokens": 9_000,
        },
        request_id="req_123",
    )
    assert len(fake_db) == 1
    row = fake_db[0]
    assert row.org_id == "org-a"
    assert row.user_id == user_id
    assert row.feature is LlmFeature.chat
    assert row.provider == "anthropic"
    assert row.model == "claude-opus-5-5"
    assert (row.input_tokens, row.output_tokens) == (100, 350)
    assert (row.cache_creation_input_tokens, row.cache_read_input_tokens) == (1_200, 9_000)
    assert row.cost_usd == Decimal("0.015200")  # 400 + 1800 + 6000 + 7000 = 15,200
    assert row.request_id == "req_123"
    assert isinstance(row.resource_id, str)


async def test_record_usage_defaults_provider_and_allows_missing_user(fake_db, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    await usage.record_usage(
        org_id="org-a",
        user_id=None,
        feature=LlmFeature.csr_narrative,
        resource_type=None,
        resource_id=None,
        provider=None,
        model="claude-opus-4-6",
        usage=None,
        request_id=None,
    )
    row = fake_db[0]
    assert row.provider == "anthropic"
    assert row.user_id is None
    assert row.input_tokens == 0 and row.cost_usd == Decimal("0")


async def test_record_usage_swallows_a_database_failure(monkeypatch, caplog):
    monkeypatch.setattr(
        database, "AsyncSessionLocal", lambda: _FakeSession([], fail_commit=True)
    )
    with caplog.at_level(logging.WARNING, logger="services.llm.usage"):
        result = await usage.record_usage(
            org_id="org-a", feature="chat", model="claude-opus-5-5", usage={"input_tokens": 1},
        )
    assert result is None  # no exception propagated
    assert any("Failed to record LLM usage" in r.getMessage() for r in caplog.records)


async def test_record_usage_swallows_bad_input(fake_db):
    # unknown feature, missing org, missing model: each logged and swallowed
    await usage.record_usage(org_id="o", feature="not-a-feature", model="claude-opus-5-5", usage={})
    await usage.record_usage(org_id="", feature="chat", model="claude-opus-5-5", usage={})
    await usage.record_usage(org_id="o", feature="chat", model="", usage={})
    assert fake_db == []


async def test_record_usage_swallows_session_factory_errors(monkeypatch):
    def _boom():
        raise RuntimeError("no engine")

    monkeypatch.setattr(database, "AsyncSessionLocal", _boom)
    await usage.record_usage(
        org_id="org-a", feature="chat", model="claude-opus-5-5", usage={"input_tokens": 1},
    )


async def test_record_usage_tolerates_a_non_uuid_user_id(fake_db):
    await usage.record_usage(
        org_id="org-a", user_id="not-a-uuid", feature="chat", model="claude-opus-5-5", usage={},
    )
    assert len(fake_db) == 1 and fake_db[0].user_id is None


async def test_record_usage_clips_long_strings(fake_db):
    await usage.record_usage(
        org_id="org-a",
        feature="chat",
        resource_type="x" * 100,
        model="m" * 200,
        usage={},
        request_id="r" * 500,
    )
    row = fake_db[0]
    assert len(row.resource_type) == 32
    assert len(row.model) == 64
    assert len(row.request_id) == 128
