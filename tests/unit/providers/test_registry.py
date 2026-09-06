"""Tests for the per-spec AsyncAnthropic client constructor."""

from __future__ import annotations

import pytest
from anthropic import AsyncAnthropic

from lies.providers.config import ProviderSpec
from lies.providers.errors import ProviderConfigError
from lies.providers.registry import _client_for


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


def test_returns_fresh_instance_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """N5: no @cache — each call constructs a fresh client so live env-var
    rotation is picked up. This replaces the previous
    ``test_cache_returns_same_instance`` invariant."""
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    spec = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/anthropic",
    )
    a = _client_for(spec)
    b = _client_for(spec)
    assert a is not b


# ---------------------------------------------------------------------------
# N5: live env-var re-read so token rotation is picked up without restart
# ---------------------------------------------------------------------------


def test_live_re_read_picks_new_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh _client_for call after env var rotation must construct a client
    holding the new value. The @cache decorator must not freeze the old one."""
    monkeypatch.setenv("MINIMAX_API_KEY", "key-v1")
    spec = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/anthropic",
    )
    first = _client_for(spec)
    assert first.api_key == "key-v1"

    monkeypatch.setenv("MINIMAX_API_KEY", "key-v2-rotated")
    second = _client_for(spec)
    assert second.api_key == "key-v2-rotated"
