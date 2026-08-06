"""Tests for the per-spec AsyncAnthropic client cache."""

from __future__ import annotations

import pytest
from anthropic import AsyncAnthropic

from lies.providers.config import ProviderSpec
from lies.providers.errors import ProviderConfigError
from lies.providers.registry import _client_for


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    from lies.providers.registry import _client_for

    _client_for.cache_clear()


def test_builds_client_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    spec = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/anthropic",
    )
    client = _client_for(spec)
    assert isinstance(client, AsyncAnthropic)


def test_raises_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    spec = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/anthropic",
    )
    with pytest.raises(ProviderConfigError, match="MINIMAX_API_KEY"):
        _client_for(spec)


def test_cache_returns_same_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    spec = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/anthropic",
    )
    a = _client_for(spec)
    b = _client_for(spec)
    assert a is b
