"""Tests for the companion subcommand bodies in ``ops.py``."""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.providers.agents import AGENT_ROSTER
from lies.providers.bootstrap import PartialConfig, write_atomic
from lies.providers.config import ProviderSpec, load_providers_config
from lies.providers.errors import ProviderConfigError
from lies.providers.ops import (
    ProvidersConfigMissing,
    add_provider,
    assign_agent,
    check_connectivity,
    set_default_model,
    unassign_agent,
)


def _seed_target(tmp_path: Path, extra_agents: dict[str, str] | None = None) -> Path:
    target = tmp_path / "providers.toml"
    agents = {name: "anthropic:claude-opus-4-7" for name in AGENT_ROSTER}
    agents.update(extra_agents or {})
    write_atomic(
        target,
        PartialConfig(
            providers={
                "anthropic": ProviderSpec(
                    name="anthropic",
                    type="anthropic",
                    api_key_env="ANTHROPIC_API_KEY",
                ),
            },
            default_model="anthropic:claude-opus-4-7",
            agents=agents,
        ),
    )
    return target


def test_add_provider_round_trip(tmp_path: Path) -> None:
    target = _seed_target(tmp_path)
    new_spec = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/anthropic",
    )
    add_provider(target, new_spec)
    loaded = load_providers_config(target)
    assert loaded is not None
    assert "minimax" in loaded.providers


def test_set_default_model_round_trip(tmp_path: Path) -> None:
    target = _seed_target(tmp_path)
    set_default_model(target, "anthropic:claude-opus-4-7")
    loaded = load_providers_config(target)
    assert loaded is not None
    assert loaded.default_model == "anthropic:claude-opus-4-7"


def test_assign_agent_round_trip(tmp_path: Path) -> None:
    target = _seed_target(tmp_path)
    assign_agent(target, "source_reader", "anthropic:claude-opus-4-7")
    loaded = load_providers_config(target)
    assert loaded is not None
    assert loaded.agents["source_reader"] == "anthropic:claude-opus-4-7"


def test_unassign_agent_raises_for_roster_member(tmp_path: Path) -> None:
    target = _seed_target(tmp_path)
    with pytest.raises(ProviderConfigError, match="would leave the AGENT_ROSTER incomplete"):
        unassign_agent(target, "source_reader")


def test_unassign_agent_succeeds_for_extra_agent(tmp_path: Path) -> None:
    target = _seed_target(tmp_path, extra_agents={"legacy_agent": "anthropic:claude-opus-4-7"})
    unassign_agent(target, "legacy_agent")
    loaded = load_providers_config(target)
    assert loaded is not None
    assert "legacy_agent" not in loaded.agents
    assert all(name in loaded.agents for name in AGENT_ROSTER)


def test_companion_missing_file_raises(tmp_path: Path) -> None:
    target = tmp_path / "nope.toml"
    new_spec = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/anthropic",
    )
    with pytest.raises(ProvidersConfigMissing):
        add_provider(target, new_spec)


def test_check_connectivity_anthropic_compatible_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")

    class _FakeMessages:
        @staticmethod
        async def create(*args, **kwargs):
            class _Resp:
                pass

            return _Resp()

    class _FakeAnthropic:
        def __init__(self, base_url, api_key):
            self.base_url = base_url
            self.api_key = api_key
            self.messages = _FakeMessages()

    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)

    target = _seed_target(tmp_path)
    write_atomic(
        target,
        PartialConfig(
            providers={
                "minimax": ProviderSpec(
                    name="minimax",
                    type="anthropic_compatible",
                    api_key_env="MINIMAX_API_KEY",
                    base_url="https://api.minimax.io/anthropic",
                ),
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
                "linter": "anthropic:claude-opus-4-7",
                "query_synthesizer": "anthropic:claude-opus-4-7",
                "enricher": "anthropic:claude-opus-4-7",
                "repair": "anthropic:claude-opus-4-7",
            },
        ),
    )
    status = check_connectivity(target)
    by_name = {name: st for name, st, _ in status}
    assert by_name["minimax"] == "ok"


def test_check_connectivity_ok_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """check_connectivity returns ('minimax', 'ok', ...) when the env var is set."""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-rotation-A")

    class _FakeMessages:
        @staticmethod
        async def create(*args, **kwargs):
            class _Resp:
                pass

            return _Resp()

    class _FakeAnthropic:
        def __init__(self, base_url, api_key):
            self.base_url = base_url
            self.api_key = api_key
            self.messages = _FakeMessages()

    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-rotation-A")

    target = tmp_path / "providers.toml"
    write_atomic(
        target,
        PartialConfig(
            providers={
                "minimax": ProviderSpec(
                    name="minimax",
                    type="anthropic_compatible",
                    api_key_env="MINIMAX_API_KEY",
                    base_url="https://api.minimax.io/anthropic",
                ),
                "anthropic": ProviderSpec(
                    name="anthropic",
                    type="anthropic",
                    api_key_env="ANTHROPIC_API_KEY",
                ),
            },
            default_model="anthropic:claude-opus-4-7",
            agents={n: "anthropic:claude-opus-4-7" for n in AGENT_ROSTER},
        ),
    )
    rows = check_connectivity(target)
    by_name = {name: (status, detail) for name, status, detail in rows}
    assert by_name["minimax"][0] == "ok"


def test_check_connectivity_unkeyed_message_omits_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The 'unkeyed' status message names the env var but never the value."""
    secret = "supersecret-rotation-value-A"
    monkeypatch.setenv("MINIMAX_API_KEY", secret)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    target = tmp_path / "providers.toml"
    write_atomic(
        target,
        PartialConfig(
            providers={
                "minimax": ProviderSpec(
                    name="minimax",
                    type="anthropic_compatible",
                    api_key_env="MINIMAX_API_KEY",
                    base_url="https://api.minimax.io/anthropic",
                ),
                "anthropic": ProviderSpec(
                    name="anthropic",
                    type="anthropic",
                    api_key_env="ANTHROPIC_API_KEY",
                ),
            },
            default_model="anthropic:claude-opus-4-7",
            agents={n: "anthropic:claude-opus-4-7" for n in AGENT_ROSTER},
        ),
    )
    with caplog.at_level("DEBUG", logger="lies.providers.ops"):
        rows = check_connectivity(target)
    by_name = {name: (status, detail) for name, status, detail in rows}
    status, detail = by_name["minimax"]
    assert status == "unkeyed"
    assert "MINIMAX_API_KEY" in detail
    assert secret not in detail
    leaked_logs = [r for r in caplog.records if secret in r.getMessage()]
    assert leaked_logs == [], (
        f"env var VALUE leaked into log records: {[r.getMessage() for r in leaked_logs]}"
    )


def test_probe_raises_provider_config_error_on_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_probe raises ProviderConfigError (no longer KeyError) when env var unset."""
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    spec = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.io/anthropic",
    )
    from lies.providers.ops import _probe

    with pytest.raises(ProviderConfigError, match="MINIMAX_API_KEY"):
        _probe(spec)
