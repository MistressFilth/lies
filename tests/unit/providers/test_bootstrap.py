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


def test_run_wizard_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "providers.toml"
    answers = iter(
        [
            "anthropic:claude-opus-4-7",  # default_model
            "yes",  # edit providers
            # block 1: anthropic_compatible
            "minimax",
            "anthropic_compatible",
            "MINIMAX_API_KEY",
            "https://api.minimax.io/anthropic",
            "",  # stop providers loop
            "yes",  # assign default to every agent
            "yes",  # confirm write
        ]
    )

    def prompt(label: str, default: str) -> str:
        return next(answers)

    from lies.providers.bootstrap import run_wizard

    run_wizard(
        target,
        check_connection=False,
        write_env_file=None,
        non_interactive=False,
        prompt=prompt,
    )
    from lies.providers.config import load_providers_config

    loaded = load_providers_config(target)
    assert loaded is not None
    assert "minimax" in loaded.providers


def test_run_wizard_aborts_leave_file_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "providers.toml"
    answers = iter(["bogus:provider", "no"])  # bad model + decline continue

    def prompt(label: str, default: str) -> str:
        return next(answers)

    from lies.providers.bootstrap import run_wizard

    with pytest.raises(BootstrapAborted):
        run_wizard(
            target,
            check_connection=False,
            write_env_file=None,
            non_interactive=False,
            prompt=prompt,
        )
    assert not target.exists()


def test_run_wizard_writes_env_file_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full happy path with `--write-env-file` set; the operator must
    declare the minimax provider through the wizard so its key is a
    legitimate capture target — the env file iterates declared
    providers, not unrelated shell values."""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test-123")
    env_path = tmp_path / "lies.env"
    target = tmp_path / "providers.toml"

    answers = iter(
        [
            "anthropic:claude-opus-4-7",  # default_model
            "yes",  # edit providers
            # block 1: anthropic_compatible
            "minimax",
            "anthropic_compatible",
            "MINIMAX_API_KEY",
            "https://api.minimax.io/anthropic",
            "",  # stop providers loop
            "yes",  # assign default to every agent
            "yes",  # confirm write
        ]
    )

    def prompt(label: str, default: str) -> str:
        return next(answers)

    from lies.providers.bootstrap import run_wizard

    run_wizard(
        target,
        check_connection=False,
        write_env_file=env_path,
        non_interactive=False,
        prompt=prompt,
    )
    assert env_path.exists()
    mode = env_path.stat().st_mode & 0o777
    assert mode == 0o600
    text = env_path.read_text()
    assert "MINIMAX_API_KEY=sk-test-123" in text


def test_run_wizard_reload_error_aborts_no_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reload failure must abort before env capture: when the wizard
    writes a providers.toml that ``load_providers_config`` rejects (an
    incomplete roster, here from skipping the agents step), the
    operator should see the typed abort, the file should be left on
    disk, and the env capture file must NOT exist — because reload
    abort short-circuits env capture.
    """
    env_path = tmp_path / "lies.env"
    target = tmp_path / "providers.toml"

    answers = iter(
        [
            "anthropic:claude-opus-4-7",  # default_model
            "yes",  # edit providers
            # block 1: anthropic_compatible
            "minimax",
            "anthropic_compatible",
            "MINIMAX_API_KEY",
            "https://api.minimax.io/anthropic",
            "",  # stop providers loop
            "no",  # decline agents assignment (leaves [agents] empty → reload rejects)
            "yes",  # confirm write (reload will fire after this and abort)
        ]
    )

    def prompt(label: str, default: str) -> str:
        return next(answers)

    from lies.providers.bootstrap import run_wizard

    with pytest.raises(BootstrapAborted):
        run_wizard(
            target,
            check_connection=False,
            write_env_file=env_path,
            non_interactive=False,
            prompt=prompt,
        )

    # write_atomic completed before reload — file should exist.
    assert target.exists()
    # Reload should reject (no [agents] entries).
    from lies.providers.config import load_providers_config
    from lies.providers.errors import ProviderConfigError

    with pytest.raises(ProviderConfigError):
        load_providers_config(target)
    # Env capture must NOT have been written — reload abort short-circuits
    # the env capture step.
    assert not env_path.exists()
