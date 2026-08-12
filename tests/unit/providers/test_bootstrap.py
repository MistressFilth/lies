"""Tests for the bootstrap module: atomic writer + typed aborts + wizard steps."""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.providers.bootstrap import (
    BootstrapAborted,
    BootstrapValidationFailed,
    PartialConfig,
    ProvidersConfigMissing,
    detect_env_keys,
    step_agents,
    step_default_model,
    step_providers,
    write_atomic,
)
from lies.providers.config import ProviderSpec


def _partial() -> PartialConfig:
    return PartialConfig(
        providers={
            "anthropic": ProviderSpec(
                name="anthropic",
                type="anthropic",
                api_key_env="ANTHROPIC_API_KEY",
            ),
        },
        default_model="anthropic:claude-opus-4-7",
        agents={
            "orchestrator": "anthropic:claude-opus-4-7",
            "source_reader": "anthropic:claude-opus-4-7",
            "page_writer": "anthropic:claude-opus-4-7",
            "indexer": "anthropic:claude-opus-4-7",
            "linter": "anthropic:claude-opus-4-7",
            "query_synthesizer": "anthropic:claude-opus-4-7",
            "enricher": "anthropic:claude-opus-4-7",
            "repair": "anthropic:claude-opus-4-7",
        },
    )


def test_write_atomic_creates_file_with_perms_0600(tmp_path: Path) -> None:
    target = tmp_path / "providers.toml"
    write_atomic(target, _partial())
    assert target.exists()
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_atomic_round_trip_load(tmp_path: Path) -> None:
    target = tmp_path / "providers.toml"
    write_atomic(target, _partial())
    from lies.providers.config import load_providers_config

    loaded = load_providers_config(target)
    assert loaded is not None
    assert loaded.default_model == "anthropic:claude-opus-4-7"


def test_write_atomic_overwrite_existing(tmp_path: Path) -> None:
    target = tmp_path / "providers.toml"
    target.write_text("# stale content that will be replaced")
    write_atomic(target, _partial())
    text = target.read_text()
    assert "stale content" not in text
    assert "[providers.anthropic]" in text


def test_exceptions_are_distinct() -> None:
    """Each typed exception is its own class; hierarchy is correct."""
    assert issubclass(BootstrapValidationFailed, BootstrapAborted)
    assert not issubclass(BootstrapAborted, ProvidersConfigMissing)
    assert not issubclass(ProvidersConfigMissing, BootstrapAborted)


def _partial_min() -> PartialConfig:
    return PartialConfig(
        providers={
            "anthropic": ProviderSpec(
                name="anthropic",
                type="anthropic",
                api_key_env="ANTHROPIC_API_KEY",
            ),
        },
        default_model=None,
        agents={},
    )


def test_detect_env_keys_known_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = detect_env_keys()
    assert result["ANTHROPIC_API_KEY"] is True
    assert result["MINIMAX_API_KEY"] is False
    assert result["OPENAI_API_KEY"] is False
    assert result["GEMINI_API_KEY"] is False


def test_detect_env_keys_returns_all_four_names() -> None:
    result = detect_env_keys()
    assert set(result) == {
        "ANTHROPIC_API_KEY",
        "MINIMAX_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
    }
    assert all(isinstance(v, bool) for v in result.values())


def test_step_default_model_accepts_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = _partial_min()
    answers = iter(["anthropic:claude-opus-4-7"])

    def prompt(label: str, default: str) -> str:
        return next(answers)

    step_default_model(partial, prompt=prompt)
    assert partial.default_model == "anthropic:claude-opus-4-7"


def test_step_default_model_rejects_unknown_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lies.providers.errors import ProviderConfigError

    partial = _partial_min()
    answers = iter(["minimax:MiniMax-M3"])

    def prompt(label: str, default: str) -> str:
        return next(answers)

    with pytest.raises(ProviderConfigError, match="undeclared provider 'minimax'"):
        step_default_model(partial, prompt=prompt)


def test_step_providers_appends_minimax(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = _partial_min()
    answers = iter(
        [
            "minimax",  # provider name
            "anthropic_compatible",  # type
            "MINIMAX_API_KEY",  # api_key_env
            "https://api.minimax.io/anthropic",  # base_url
            "",  # blank -> stop loop
        ]
    )

    def prompt(label: str, default: str) -> str:
        return next(answers)

    step_providers(partial, prompt=prompt)
    assert "minimax" in partial.providers
    assert partial.providers["minimax"].type == "anthropic_compatible"
    assert partial.providers["minimax"].base_url == "https://api.minimax.io/anthropic"


def test_step_agents_mirrors_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = _partial_min()
    partial.default_model = "anthropic:claude-opus-4-7"

    answers = iter(["yes"])

    def prompt(label: str, default: str) -> str:
        return next(answers)

    step_agents(partial, prompt=prompt)
    from lies.providers.agents import AGENT_ROSTER

    for name in AGENT_ROSTER:
        assert partial.agents[name] == "anthropic:claude-opus-4-7"


def test_step_agents_skip_keeps_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = _partial_min()
    partial.default_model = "anthropic:claude-opus-4-7"
    answers = iter(["no"])

    def prompt(label: str, default: str) -> str:
        return next(answers)

    step_agents(partial, prompt=prompt)
    assert partial.agents == {}
