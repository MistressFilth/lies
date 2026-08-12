"""Tests for the companion subcommand bodies in ``ops.py``."""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.providers.bootstrap import PartialConfig, write_atomic
from lies.providers.config import ProviderSpec, load_providers_config
from lies.providers.ops import (
    ProvidersConfigMissing,
    add_provider,
    assign_agent,
    check_connectivity,
    set_default_model,
    unassign_agent,
)


def _seed_target(tmp_path: Path) -> Path:
    target = tmp_path / "providers.toml"
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


def test_unassign_agent_round_trip(tmp_path: Path) -> None:
    target = _seed_target(tmp_path)
    unassign_agent(target, "source_reader")
    loaded = load_providers_config(target)
    assert loaded is not None
    assert "source_reader" not in loaded.agents


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


def test_check_connectivity_status_per_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    target = _seed_target(tmp_path)
    status = check_connectivity(target)
    assert isinstance(status, list)
    assert any(name == "anthropic" and st == "unkeyed" or st == "ok" for name, st, _ in status)
