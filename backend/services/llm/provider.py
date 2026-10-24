"""LLM provider seam: which client, which model id, which API features.

Only the chat feature goes through this at first; moving ai_service /
three_stage_pipeline / csr_pipeline onto it (and writing their usage to the
ledger) is a later step. The seam exists so the AWS migration can flip
LLM_PROVIDER=bedrock without touching call sites.

Everything reads the environment at CALL time, never at import time, so tests
(and a restarted worker) see the current values.

Environment:
  LLM_PROVIDER         "anthropic" (default) or "bedrock"
  CHAT_MODEL           default claude-opus-5-5
  GENERATION_MODEL     default claude-sonnet-4-6
  CSR_NARRATIVE_MODEL  default claude-opus-4-6
  EXTRACTION_MODEL     default claude-sonnet-4-6
  ANTHROPIC_API_KEY    used by the "anthropic" provider (same as the existing code)
  AWS_REGION / AWS_DEFAULT_REGION   Bedrock region (default us-east-1)

Model names are written in first-party form (claude-opus-5-5). On Bedrock the
Messages-API ("Mantle") endpoint wants an `anthropic.` prefix
(anthropic.claude-opus-5-5); model_id() adds it unless the configured id already
carries a provider prefix. The older bedrock-runtime path needs cross-region
inference-profile ids instead - re-verify at migration time.

Bedrock needs boto3/botocore to sign requests (`pip install "anthropic[bedrock]"`).
Constructing the client does not need them; the first request does. They are not
installed in this project yet - add them when the Bedrock switch is made.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import anthropic

from services.llm.pricing import normalize_model_id

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_BEDROCK = "bedrock"
_PROVIDERS = (PROVIDER_ANTHROPIC, PROVIDER_BEDROCK)

DEFAULT_BEDROCK_REGION = "us-east-1"

# logical name -> (env var, default model id)
_MODEL_CONFIG: dict[str, tuple[str, str]] = {
    "chat": ("CHAT_MODEL", "claude-opus-5-5"),
    "generation": ("GENERATION_MODEL", "claude-sonnet-4-6"),
    "csr": ("CSR_NARRATIVE_MODEL", "claude-opus-4-6"),
    "extraction": ("EXTRACTION_MODEL", "claude-sonnet-4-6"),
}
LOGICAL_MODELS: tuple[str, ...] = tuple(_MODEL_CONFIG)


def provider_name() -> str:
    """The configured provider: "anthropic" (default) or "bedrock".

    An unrecognised LLM_PROVIDER raises ValueError - a misspelt provider must
    fail loudly at configuration time, not silently fall back to the wrong one.
    """
    raw = (os.getenv("LLM_PROVIDER") or PROVIDER_ANTHROPIC).strip().lower()
    if raw not in _PROVIDERS:
        raise ValueError(
            f"Unsupported LLM_PROVIDER {raw!r}; expected one of {', '.join(_PROVIDERS)}."
        )
    return raw


def bedrock_region() -> str:
    return (
        os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or DEFAULT_BEDROCK_REGION
    ).strip()


def get_async_client(*, max_retries: int | None = None, **client_kwargs: Any) -> Any:
    """An async Messages-API client for the configured provider.

    "anthropic" -> anthropic.AsyncAnthropic(api_key=$ANTHROPIC_API_KEY)
    "bedrock"   -> anthropic.AsyncAnthropicBedrockMantle(aws_region=...)

    The anthropic module attribute is looked up here, at call time, so patching
    `anthropic.AsyncAnthropic` (as tests/conftest.py's mock_anthropic does)
    takes effect. Constructing a client makes no network call.
    """
    if max_retries is not None:
        client_kwargs["max_retries"] = max_retries

    if provider_name() == PROVIDER_BEDROCK:
        client_kwargs.setdefault("aws_region", bedrock_region())
        return anthropic.AsyncAnthropicBedrockMantle(**client_kwargs)

    client_kwargs.setdefault("api_key", os.getenv("ANTHROPIC_API_KEY"))
    return anthropic.AsyncAnthropic(**client_kwargs)


def _has_provider_prefix(model: str) -> bool:
    return (
        model.startswith("anthropic.")
        or ".anthropic." in model
        or model.startswith("arn:")
    )


def model_id(logical: str) -> str:
    """Model id for a logical use: "chat", "generation", "csr" or "extraction".

    Reads the matching env var (blank counts as unset) and falls back to the
    default. On Bedrock, adds the `anthropic.` prefix unless one is present.
    """
    try:
        env_name, default = _MODEL_CONFIG[logical]
    except KeyError:
        raise ValueError(
            f"Unknown logical model {logical!r}; expected one of {', '.join(LOGICAL_MODELS)}."
        ) from None
    model = (os.getenv(env_name) or "").strip() or default
    if provider_name() == PROVIDER_BEDROCK and not _has_provider_prefix(model):
        model = f"anthropic.{model}"
    return model


# --------------------------------------------------------------------------
# Capability flags
# --------------------------------------------------------------------------

# Models for which the orchestrator should request server-side refusal
# fallbacks (`fallbacks: "default"`, beta header server-side-fallback-2026-07-01).
# The feature is Claude API only - not available on Bedrock.
_SERVER_FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)

# Models that accept a `role: "system"` message inside `messages[]` (so the
# per-turn draft-spec snapshot can be added without invalidating the cached
# history prefix). Available on the Claude API and on Bedrock. Not Claude
# Sonnet 5, and not the 4.6 models.
_MID_CONVERSATION_SYSTEM_MODELS = frozenset(
    {
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-fable-5-1",
        "claude-fable-5",
        "claude-mythos-5-1",
        "claude-mythos-5",
    }
)


@dataclass(frozen=True)
class LlmCapabilities:
    provider: str
    model: str
    supports_server_fallbacks: bool
    supports_mid_conversation_system: bool


def capabilities(logical: str = "chat") -> LlmCapabilities:
    """Capability flags for the model configured for `logical` on the configured
    provider. Conservative: an unknown or custom model id gets both flags False."""
    provider = provider_name()
    model = model_id(logical)
    base = normalize_model_id(model)
    return LlmCapabilities(
        provider=provider,
        model=model,
        supports_server_fallbacks=(
            provider == PROVIDER_ANTHROPIC and base in _SERVER_FALLBACK_MODELS
        ),
        supports_mid_conversation_system=base in _MID_CONVERSATION_SYSTEM_MODELS,
    )


def supports_server_fallbacks(logical: str = "chat") -> bool:
    return capabilities(logical).supports_server_fallbacks


def supports_mid_conversation_system(logical: str = "chat") -> bool:
    return capabilities(logical).supports_mid_conversation_system
