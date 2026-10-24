"""Pure tests for the LLM provider seam (services/llm/provider.py).

No database, no network: clients are only CONSTRUCTED (the SDK makes no request
at construction), and the `httpx_mock` fixture is requested in the
client-building tests so any accidental outbound request would fail the test.
"""
from __future__ import annotations

import anthropic
import pytest

from services.llm import provider
from services.llm.provider import (
    LOGICAL_MODELS,
    capabilities,
    get_async_client,
    model_id,
    provider_name,
    supports_mid_conversation_system,
    supports_server_fallbacks,
)

_ENV_VARS = (
    "LLM_PROVIDER",
    "CHAT_MODEL",
    "GENERATION_MODEL",
    "CSR_NARRATIVE_MODEL",
    "EXTRACTION_MODEL",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with none of the seam's env vars set (a developer's
    real .env must not leak in)."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def test_default_provider_is_anthropic():
    assert provider_name() == "anthropic"


def test_provider_name_is_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "  Bedrock ")
    assert provider_name() == "bedrock"


def test_unknown_provider_fails_loudly(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        provider_name()
    with pytest.raises(ValueError):
        get_async_client()


def test_default_client_is_async_anthropic(monkeypatch, httpx_mock):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    client = get_async_client()
    assert isinstance(client, anthropic.AsyncAnthropic)
    assert not isinstance(client, anthropic.AsyncAnthropicBedrockMantle)
    assert client.api_key == "sk-ant-test-not-real"


def test_default_client_accepts_max_retries(monkeypatch, httpx_mock):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    assert get_async_client(max_retries=4).max_retries == 4


def test_client_is_looked_up_at_call_time(monkeypatch):
    """tests/conftest.py's mock_anthropic patches `anthropic.AsyncAnthropic`;
    the seam must resolve the class when called, not at import."""
    sentinel = object()
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **kw: sentinel)
    assert get_async_client() is sentinel


def test_bedrock_provider_returns_mantle_client(monkeypatch, httpx_mock):
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    client = get_async_client()
    assert isinstance(client, anthropic.AsyncAnthropicBedrockMantle)
    assert "eu-west-1" in str(client.base_url)


def test_bedrock_region_defaults_to_us_east_1(monkeypatch, httpx_mock):
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    client = get_async_client()
    assert isinstance(client, anthropic.AsyncAnthropicBedrockMantle)
    assert "us-east-1" in str(client.base_url)


def test_bedrock_region_falls_back_to_aws_default_region(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    assert provider.bedrock_region() == "ap-southeast-2"
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert provider.bedrock_region() == "us-west-2"  # AWS_REGION wins


# ---------------------------------------------------------------------------
# model_id
# ---------------------------------------------------------------------------


def test_default_model_ids():
    assert model_id("chat") == "claude-opus-5-5"
    assert model_id("generation") == "claude-sonnet-4-6"
    assert model_id("csr") == "claude-opus-4-6"
    assert model_id("extraction") == "claude-sonnet-4-6"


@pytest.mark.parametrize(
    "logical, env_name",
    [
        ("chat", "CHAT_MODEL"),
        ("generation", "GENERATION_MODEL"),
        ("csr", "CSR_NARRATIVE_MODEL"),
        ("extraction", "EXTRACTION_MODEL"),
    ],
)
def test_model_id_env_overrides(monkeypatch, logical, env_name):
    monkeypatch.setenv(env_name, "claude-sonnet-5-5")
    assert model_id(logical) == "claude-sonnet-5-5"


def test_model_id_override_is_independent_per_logical_name(monkeypatch):
    monkeypatch.setenv("CHAT_MODEL", "claude-haiku-5-5")
    assert model_id("chat") == "claude-haiku-5-5"
    assert model_id("generation") == "claude-sonnet-4-6"  # untouched


def test_blank_env_value_uses_default(monkeypatch):
    monkeypatch.setenv("CHAT_MODEL", "   ")
    assert model_id("chat") == "claude-opus-5-5"


def test_unknown_logical_name_raises():
    with pytest.raises(ValueError, match="Unknown logical model"):
        model_id("bogus")


def test_every_logical_name_resolves():
    for logical in LOGICAL_MODELS:
        assert model_id(logical).startswith("claude-")


def test_bedrock_adds_anthropic_prefix(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    assert model_id("chat") == "anthropic.claude-opus-5-5"
    assert model_id("generation") == "anthropic.claude-sonnet-4-6"


def test_bedrock_does_not_double_prefix(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("CHAT_MODEL", "anthropic.claude-opus-5-5")
    assert model_id("chat") == "anthropic.claude-opus-5-5"
    monkeypatch.setenv("CSR_NARRATIVE_MODEL", "us.anthropic.claude-opus-4-6-v1:0")
    assert model_id("csr") == "us.anthropic.claude-opus-4-6-v1:0"


# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------


def test_default_chat_model_capabilities():
    caps = capabilities("chat")
    assert caps.provider == "anthropic"
    assert caps.model == "claude-opus-5-5"
    assert caps.supports_server_fallbacks is True
    assert caps.supports_mid_conversation_system is True
    assert supports_server_fallbacks() is True
    assert supports_mid_conversation_system() is True


def test_server_fallbacks_are_claude_api_only(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    assert supports_server_fallbacks("chat") is False
    # Mid-conversation system messages ARE available on Bedrock.
    assert supports_mid_conversation_system("chat") is True
    assert capabilities("chat").model == "anthropic.claude-opus-5-5"


def test_older_models_have_neither_flag():
    # Defaults: generation and csr models are the 4.6 generation.
    assert supports_server_fallbacks("generation") is False
    assert supports_mid_conversation_system("generation") is False
    assert supports_server_fallbacks("csr") is False
    assert supports_mid_conversation_system("csr") is False


def test_haiku_has_mid_conversation_system_but_no_server_fallback(monkeypatch):
    monkeypatch.setenv("CHAT_MODEL", "claude-haiku-5-5")
    assert supports_mid_conversation_system("chat") is True
    assert supports_server_fallbacks("chat") is False


def test_unknown_custom_model_is_conservative(monkeypatch):
    monkeypatch.setenv("CHAT_MODEL", "some-future-model")
    caps = capabilities("chat")
    assert caps.supports_server_fallbacks is False
    assert caps.supports_mid_conversation_system is False
