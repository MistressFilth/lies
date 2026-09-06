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
                    api_key_envs=("ANTHROPIC_API_KEY",),
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
        api_key_envs=("MINIMAX_API_KEY",),
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
        api_key_envs=("MINIMAX_API_KEY",),
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
                    api_key_envs=("MINIMAX_API_KEY",),
                    base_url="https://api.minimax.io/anthropic",
                ),
                "anthropic": ProviderSpec(
                    name="anthropic",
                    type="anthropic",
                    api_key_envs=("ANTHROPIC_API_KEY",),
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


def test_check_connectivity_chain_all_missing_unkeyed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every entry in ``api_key_envs`` is unset, the row is 'unkeyed'
    and the message lists the full chain so the operator can see what's missing."""
    monkeypatch.delenv("FAKE_KEY_A", raising=False)
    monkeypatch.delenv("FAKE_KEY_B", raising=False)
    spec = ProviderSpec(
        name="x",
        type="anthropic",
        api_key_envs=("FAKE_KEY_A", "FAKE_KEY_B"),
    )
    rows = _rows_for_spec(spec)
    assert len(rows) == 1
    name, status, detail = rows[0]
    assert name == "x"
    assert status == "unkeyed"
    assert "FAKE_KEY_A" in detail
    assert "FAKE_KEY_B" in detail


def test_check_connectivity_chain_first_set_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the first chain entry is set, status is not 'unkeyed'."""
    monkeypatch.setenv("FAKE_KEY_A", "value-a")
    monkeypatch.delenv("FAKE_KEY_B", raising=False)
    spec = ProviderSpec(
        name="x",
        type="anthropic",
        api_key_envs=("FAKE_KEY_A", "FAKE_KEY_B"),
    )
    rows = _rows_for_spec(spec)
    assert len(rows) == 1
    _name, status, _detail = rows[0]
    assert status != "unkeyed"


def test_probe_chain_all_missing_raises_provider_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every chain entry is unset, ``_probe`` raises ProviderConfigError
    (not the legacy KeyError)."""
    monkeypatch.delenv("FAKE_KEY_A", raising=False)
    monkeypatch.delenv("FAKE_KEY_B", raising=False)
    spec = ProviderSpec(
        name="x",
        type="anthropic_compatible",
        api_key_envs=("FAKE_KEY_A", "FAKE_KEY_B"),
        base_url="https://example.invalid",
    )
    from lies.providers.ops import _probe

    with pytest.raises(ProviderConfigError):
        _probe(spec)


def _rows_for_spec(spec: ProviderSpec) -> list[tuple[str, str, str]]:
    """Run ``check_connectivity`` against a synthetic single-provider file and
    return its rows. Avoids the wizard round-trip; the assertion is on the
    status/message for the chain behavior, not on file persistence.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "providers.toml"
        write_atomic(
            target,
            PartialConfig(
                providers={spec.name: spec},
                default_model=f"{spec.name}:m",
                agents={name: f"{spec.name}:m" for name in AGENT_ROSTER},
            ),
        )
        return check_connectivity(target)
