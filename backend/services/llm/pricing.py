"""LLM price table and cost math for the spend ledger (llm_usage).

All prices are USD per 1,000,000 tokens, Anthropic first-party list prices, from
the claude-api skill's model table (cached 2026-10-06). Re-check before relying
on them for billing: prices change, and Bedrock is partner-priced separately, so
for provider="bedrock" the figure is an estimate at first-party rates. Token
counts are stored next to every cost, so a row can always be re-priced later.

Where each number comes from:
  claude-opus-5-5    $4 / $20; 5-minute cache write $5; 1-hour write $8;
                     cache read $0.20 (0.05x input). Stated explicitly.
  claude-sonnet-5-5  $2 / $10; 5-minute write $2.50; 1-hour write $4;
                     cache read $0.20. Stated explicitly.
  claude-haiku-5-5   $0.10 / $0.50 when the prompt is <= 100K tokens, and
                     $0.50 / $2.50 above that. Cache read 0.1x, 5-minute write
                     1.25x, 1-hour write 2x of the input rate in force.
  claude-sonnet-4-6  $3 / $15; cache read, 5-minute and 1-hour write derived
  claude-opus-4-6    $5 / $25; from the documented 0.1x / 1.25x / 2x multipliers.

"Prompt tokens" for the Haiku tier switch is input_tokens + cache_creation +
cache_read (everything the model was sent), which is how the long-prompt rate
card is documented.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_MTOK = Decimal(1_000_000)
_COST_QUANTUM = Decimal("0.000001")  # matches llm_usage.cost_usd Numeric(12, 6)


def _d(value: str) -> Decimal:
    return Decimal(value)


@dataclass(frozen=True)
class Rates:
    """One rate card, USD per 1M tokens."""

    input: Decimal
    output: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal


@dataclass(frozen=True)
class ModelPrice:
    """A model's price: a standard rate card, plus an optional card that applies
    when the prompt exceeds `long_prompt_threshold` tokens."""

    rates: Rates
    long_prompt_threshold: int | None = None
    long_prompt_rates: Rates | None = None

    def rates_for(self, prompt_tokens: int) -> Rates:
        if (
            self.long_prompt_rates is not None
            and self.long_prompt_threshold is not None
            and prompt_tokens > self.long_prompt_threshold
        ):
            return self.long_prompt_rates
        return self.rates


PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(
        Rates(_d("4.00"), _d("20.00"), _d("5.00"), _d("8.00"), _d("0.20"))
    ),
    "claude-sonnet-5-5": ModelPrice(
        Rates(_d("2.00"), _d("10.00"), _d("2.50"), _d("4.00"), _d("0.20"))
    ),
    "claude-haiku-5-5": ModelPrice(
        Rates(_d("0.10"), _d("0.50"), _d("0.125"), _d("0.20"), _d("0.01")),
        long_prompt_threshold=100_000,
        long_prompt_rates=Rates(_d("0.50"), _d("2.50"), _d("0.625"), _d("1.00"), _d("0.05")),
    ),
    "claude-sonnet-4-6": ModelPrice(
        Rates(_d("3.00"), _d("15.00"), _d("3.75"), _d("6.00"), _d("0.30"))
    ),
    "claude-opus-4-6": ModelPrice(
        Rates(_d("5.00"), _d("25.00"), _d("6.25"), _d("10.00"), _d("0.50"))
    ),
}


# --------------------------------------------------------------------------
# Model-id normalisation (Bedrock ids carry a provider prefix / version suffix)
# --------------------------------------------------------------------------

# e.g. "us.anthropic.claude-sonnet-4-6-20260101-v1:0" -> "claude-sonnet-4-6"
_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]{2,6}\.)?anthropic\.")
_VERSION_SUFFIX = re.compile(r"(?:-\d{8})?(?:-v\d+(?::\d+)?)?$")


def normalize_model_id(model: str | None) -> str:
    """Strip a Bedrock `anthropic.` / `us.anthropic.` prefix and any `-YYYYMMDD`
    / `-v1:0` suffix, and lower-case, so one price entry serves every platform's
    spelling of the same model."""
    if not model:
        return ""
    m = model.strip().lower()
    m = _BEDROCK_PREFIX.sub("", m)
    m = _VERSION_SUFFIX.sub("", m)
    return m


def get_price(model: str | None) -> ModelPrice | None:
    return PRICES.get(normalize_model_id(model))


# --------------------------------------------------------------------------
# Usage normalisation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # Portion of cache_creation_input_tokens written with the 1-hour TTL, when
    # the API reports the per-TTL breakdown (usage.cache_creation). The rest is
    # billed at the 5-minute rate.
    cache_creation_1h_input_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_int(value: Any) -> int:
    """Tolerant int: None / junk / negatives -> 0, floats truncated. (Tests pass
    MagicMock-ish objects; the SDK's usage fields are `int | None`.)"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def coerce_usage(usage: Any) -> TokenUsage:
    """Accept an SDK `Usage` object, a plain dict, or None."""
    creation_total = _as_int(_get(usage, "cache_creation_input_tokens"))
    breakdown = _get(usage, "cache_creation")
    one_hour = min(_as_int(_get(breakdown, "ephemeral_1h_input_tokens")), creation_total)
    return TokenUsage(
        input_tokens=_as_int(_get(usage, "input_tokens")),
        output_tokens=_as_int(_get(usage, "output_tokens")),
        cache_creation_input_tokens=creation_total,
        cache_read_input_tokens=_as_int(_get(usage, "cache_read_input_tokens")),
        cache_creation_1h_input_tokens=one_hour,
    )


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------

_warned_models: set[str] = set()


def cost_usd(model: str | None, usage: Any) -> Decimal:
    """USD cost of one model call, as a Decimal rounded to 6 places.

    An UNKNOWN model costs Decimal("0") (and logs a warning once per model id)
    rather than raising: the ledger must keep recording tokens for a model the
    price table does not know yet, and those rows can be re-priced afterwards.
    """
    price = get_price(model)
    tokens = coerce_usage(usage)
    if price is None:
        key = normalize_model_id(model) or "<none>"
        if key not in _warned_models:
            _warned_models.add(key)
            logger.warning("No price for model %r; recording cost_usd=0 (tokens are kept).", model)
        return Decimal("0").quantize(_COST_QUANTUM)

    r = price.rates_for(tokens.prompt_tokens)
    writes_1h = tokens.cache_creation_1h_input_tokens
    writes_5m = tokens.cache_creation_input_tokens - writes_1h
    total = (
        Decimal(tokens.input_tokens) * r.input
        + Decimal(tokens.output_tokens) * r.output
        + Decimal(writes_5m) * r.cache_write_5m
        + Decimal(writes_1h) * r.cache_write_1h
        + Decimal(tokens.cache_read_input_tokens) * r.cache_read
    ) / _MTOK
    return total.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)
