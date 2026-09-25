"""Tests for resolve_model: built-in string vs constructed AnthropicModel."""

from __future__ import annotations

import pytest
from pydantic_ai.models.anthropic import AnthropicModel

from lies.providers.config import (
    ProvidersConfig,
    ProviderSpec,
    resolve_agent_to_provider,
)
from lies.providers.resolver import resolve_model


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


def _librarian_minimax_config() -> ProvidersConfig:
    """A roster-shaped config that exercises the ``librarian`` agent lookup."""
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
        agents={
            "librarian": "minimax:MiniMax-M3[1m]",
            "linter": "minimax:MiniMax-M3",
            "orchestrator": "anthropic:claude-opus-4-7",
        },
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


# ---------------------------------------------------------------------------
# Pure-lookup tests for ``resolve_agent_to_provider``
# ---------------------------------------------------------------------------
# ``resolve_agent_to_provider`` lives in ``lies.providers.config`` and does
# not import pydantic_ai. The tests below therefore run under the default
# ``make unit-test`` (no ``--runslow``) and stay below the 0.15s call-phase
# budget enforced by ``tests/unit/conftest.py``. They cover the
# ``librarian`` → provider-spec lookup that ``mcp/grounding.py`` relies
# on — the slow integration test in ``tests/unit/mcp/test_grounding_resolution.py``
# exercises the same lookup path *plus* the full ``ground()`` wiring; this
# fast test ensures a regression in the lookup itself fails the local
# gate even when the slow test is skipped.


def test_librarian_lookup_returns_anthropic_compatible_spec() -> None:
    cfg = _librarian_minimax_config()
    provider_name, model_name, spec = resolve_agent_to_provider("librarian", cfg)
    assert provider_name == "minimax"
    assert model_name == "MiniMax-M3[1m]"
    assert spec.type == "anthropic_compatible"
    assert spec.name == "minimax"
    assert spec.base_url == "https://api.minimax.io/anthropic"


def test_lookup_raises_keyerror_when_agent_not_in_roster() -> None:
    """``config.agents`` is the source of truth; missing entries raise KeyError."""
    cfg = _librarian_minimax_config()
    with pytest.raises(KeyError):
        resolve_agent_to_provider("nonexistent_agent", cfg)


def test_lookup_raises_keyerror_when_provider_undeclared() -> None:
    """TOML validation normally catches this; defence in depth for hand-built configs."""
    # Build a config where the agent points at an undeclared provider.
    # ``load_providers_config`` would refuse this at parse time, but
    # ``resolve_agent_to_provider`` does not re-validate — the lookup
    # simply raises KeyError from ``config.providers[provider_name]``.
    cfg = ProvidersConfig(
        providers={
            "anthropic": ProviderSpec(
                name="anthropic", type="anthropic", api_key_env="ANTHROPIC_API_KEY"
            ),
        },
        default_model="anthropic:claude-opus-4-7",
        agents={"librarian": "minimax:MiniMax-M3[1m]"},
    )
    with pytest.raises(KeyError):
        resolve_agent_to_provider("librarian", cfg)


def test_lookup_env_var_overrides_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_LIBRARIAN_MODEL", "anthropic:claude-haiku-4-5")
    cfg = _librarian_minimax_config()
    provider_name, model_name, _spec = resolve_agent_to_provider("librarian", cfg)
    assert provider_name == "anthropic"
    assert model_name == "claude-haiku-4-5"
