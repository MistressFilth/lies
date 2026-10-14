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


def test_resolver_imports_pydantic_ai_only_under_type_checking() -> None:
    """``resolver.py`` must not import pydantic-ai at module scope."""
    # It imported ``pydantic_ai.models.Model`` for a return annotation, so
    # importing ``lies.providers`` cost ~930ms. The annotation is a string
    # under ``from __future__ import annotations``, so the import belongs
    # behind ``TYPE_CHECKING`` like the provider SDKs.
    #
    # Asserted statically: a runtime probe needs a fresh interpreter, which
    # the 0.15s call-phase budget rules out of the default unit run.
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "src/lies/providers/resolver.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = ast.dump(node.test)
            if "TYPE_CHECKING" in test:
                guarded.update(range(node.lineno, node.end_lineno + 1))
    # Module and class scope only: an import inside a function body is
    # already deferred and costs nothing until that branch runs.
    scope = list(tree.body)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            scope.extend(node.body)
    for node in scope:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pydantic_ai"):
            assert node.lineno in guarded, (
                f"resolver.py:{node.lineno} imports pydantic_ai at module scope, "
                "costing every importer ~930ms"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("pydantic_ai"), (
                    f"resolver.py:{node.lineno} imports pydantic_ai at module scope"
                )


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
