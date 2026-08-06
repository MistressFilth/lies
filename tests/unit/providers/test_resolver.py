"""Tests for resolve_model: built-in string vs constructed AnthropicModel."""

from __future__ import annotations

import pytest
from pydantic_ai.models.anthropic import AnthropicModel

from lies.providers.config import ProvidersConfig, ProviderSpec
from lies.providers.resolver import resolve_model


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    from lies.providers.registry import _client_for

    _client_for.cache_clear()


def _anthropic_only_config() -> ProvidersConfig:
    return ProvidersConfig(
        providers={
            "anthropic": ProviderSpec(
                name="anthropic", type="anthropic", api_key_env="ANTHROPIC_API_KEY"
            ),
        },
        default_model="anthropic:claude-opus-4-7",
        agents={"linter": "anthropic:claude-sonnet-4-6"},
    )


def _anthropic_plus_minimax_config() -> ProvidersConfig:
    return ProvidersConfig(
        providers={
            "anthropic": ProviderSpec(
                name="anthropic", type="anthropic", api_key_env="ANTHROPIC_API_KEY"
            ),
            "minimax": ProviderSpec(
                name="minimax",
                type="anthropic_compatible",
                api_key_env="MINIMAX_API_KEY",
                base_url="https://api.minimax.io/anthropic",
            ),
        },
        default_model="anthropic:claude-opus-4-7",
        agents={"linter": "minimax:MiniMax-M3"},
    )


def test_built_in_anthropic_returns_string() -> None:
    cfg = _anthropic_only_config()
    resolved = resolve_model("linter", cfg)
    assert resolved == "anthropic:claude-sonnet-4-6"


def test_anthropic_compatible_returns_anthropic_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    cfg = _anthropic_plus_minimax_config()
    resolved = resolve_model("linter", cfg)
    assert isinstance(resolved, AnthropicModel)


def test_anthropic_compatible_raises_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from lies.providers.errors import ProviderConfigError

    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    cfg = _anthropic_plus_minimax_config()
    with pytest.raises(ProviderConfigError, match="MINIMAX_API_KEY"):
        resolve_model("linter", cfg)


def test_env_var_overrides_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_LINTER_MODEL", "anthropic:claude-haiku-4-5")
    cfg = _anthropic_only_config()
    assert resolve_model("linter", cfg) == "anthropic:claude-haiku-4-5"
