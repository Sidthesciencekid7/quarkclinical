"""Spend-ledger writer: one `llm_usage` row per model call.

record_usage() is deliberately fire-and-forget safe:

  * It opens its OWN database session. It never shares the caller's, so a ledger
    write cannot be rolled back with, expire the objects of, or hold a
    transaction open inside the request that made the model call.
  * It looks the session factory up on the `database` module at CALL time
    (`database.AsyncSessionLocal`), not via `from database import ...`, so the
    test fixture that swaps `database.AsyncSessionLocal` for the test engine
    applies here too.
  * It never raises into the caller. Any failure (bad input, DB down, a CHECK
    violation) is logged and swallowed - losing one ledger row must never fail
    a user's chat turn or a generation. (asyncio cancellation is not an error and
    still propagates.)
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import database
from models import LlmFeature, LlmUsage
from services.llm.pricing import coerce_usage, cost_usd
from services.llm.provider import provider_name

logger = logging.getLogger(__name__)


def _clip(value: str | None, length: int) -> str | None:
    if value is None:
        return None
    return str(value)[:length]


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        logger.warning("record_usage: user_id %r is not a UUID; recording NULL.", value)
        return None


def _coerce_feature(feature: Any) -> LlmFeature:
    if isinstance(feature, LlmFeature):
        return feature
    # ValueError (caught by record_usage) if it is not a known feature.
    return LlmFeature(str(feature))


async def record_usage(
    *,
    org_id: str,
    user_id: Any = None,
    feature: Any,
    resource_type: str | None = None,
    resource_id: Any = None,
    provider: str | None = None,
    model: str,
    usage: Any,
    request_id: str | None = None,
) -> None:
    """Write one ledger row. Never raises.

    `usage` may be the SDK's Usage object, a plain dict with the same keys, or
    None. `provider` defaults to the configured LLM_PROVIDER. The cost is
    computed from services.llm.pricing at write time.
    """
    try:
        if not org_id:
            raise ValueError("org_id is required")
        if not model:
            raise ValueError("model is required")
        tokens = coerce_usage(usage)
        row = LlmUsage(
            id=uuid.uuid4(),
            org_id=str(org_id)[:128],
            user_id=_uuid_or_none(user_id),
            feature=_coerce_feature(feature),
            resource_type=_clip(resource_type, 32),
            resource_id=_clip(None if resource_id is None else str(resource_id), 255),
            provider=_clip(provider or provider_name(), 32),
            model=str(model)[:64],
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            cache_creation_input_tokens=tokens.cache_creation_input_tokens,
            cache_read_input_tokens=tokens.cache_read_input_tokens,
            cost_usd=cost_usd(model, usage),
            request_id=_clip(request_id, 128),
        )
        async with database.AsyncSessionLocal() as session:
            session.add(row)
            await session.commit()
    except Exception:  # noqa: BLE001 - the ledger must never break its caller
        logger.warning(
            "Failed to record LLM usage (org=%s feature=%s model=%s); continuing.",
            org_id, feature, model, exc_info=True,
        )
