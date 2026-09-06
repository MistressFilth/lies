"""Tests for the pure TOML transform layer for providers.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.providers.agents import AGENT_ROSTER
from lies.providers.config import ProvidersConfig, ProviderSpec
from lies.providers.editor import ProvidersMutations, apply_mutations, to_toml
from lies.providers.errors import ProviderConfigError


def _base() -> ProvidersConfig:
    return ProvidersConfig(
        providers={
            "anthropic": ProviderSpec(
                name="anthropic", type="anthropic", api_key_envs=("ANTHROPIC_API_KEY",)
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
    )


def test_apply_mutations_add_provider_round_trip() -> None:
    cfg = _base()
    new = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_envs=("MINIMAX_API_KEY",),
        base_url="https://api.minimax.io/anthropic",
    )
    out = apply_mutations(cfg, ProvidersMutations(add_provider=new))
    assert "minimax" in out.providers
    assert out.providers["minimax"].base_url == "https://api.minimax.io/anthropic"


def test_apply_mutations_set_default() -> None:
    cfg = _base()
    new = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_envs=("MINIMAX_API_KEY",),
        base_url="https://api.minimax.io/anthropic",
    )
    cfg = apply_mutations(cfg, ProvidersMutations(add_provider=new))
    out = apply_mutations(cfg, ProvidersMutations(set_default="minimax:MiniMax-M3"))
    assert out.default_model == "minimax:MiniMax-M3"


def test_apply_mutations_set_default_unknown_provider_raises() -> None:
    cfg = _base()
    with pytest.raises(ProviderConfigError, match="undeclared provider 'minimax'"):
        apply_mutations(cfg, ProvidersMutations(set_default="minimax:MiniMax-M3"))


def test_apply_mutations_assign_agent() -> None:
    cfg = _base()
    new = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_envs=("MINIMAX_API_KEY",),
        base_url="https://api.minimax.io/anthropic",
    )
    cfg = apply_mutations(cfg, ProvidersMutations(add_provider=new))
    out = apply_mutations(
        cfg,
        ProvidersMutations(
            set_agents={"source_reader": "minimax:MiniMax-M3"},
        ),
    )
    assert out.agents["source_reader"] == "minimax:MiniMax-M3"


def test_apply_mutations_add_provider_duplicate_raises() -> None:
    cfg = _base()
    dup = ProviderSpec(name="anthropic", type="anthropic", api_key_envs=("ANTHROPIC_API_KEY",))
    with pytest.raises(ProviderConfigError, match="already declared"):
        apply_mutations(cfg, ProvidersMutations(add_provider=dup))


def test_to_toml_canonical_key_order() -> None:
    cfg = _base()
    text = to_toml(cfg)
    default_idx = text.index("default_model")
    providers_idx = text.index("[providers.")
    agents_idx = text.index("[agents]")
    assert default_idx < providers_idx < agents_idx


def test_to_toml_round_trip_load() -> None:
    from lies.providers.config import load_providers_config

    cfg = _base()
    path = Path("/tmp/_lies_toml_round_trip.toml")
    path.write_text(to_toml(cfg))
    try:
        loaded = load_providers_config(path)
    finally:
        path.unlink(missing_ok=True)
    assert loaded == cfg


def test_toml_serializes_list() -> None:
    """``to_toml(spec)`` emits the api_key_envs list literal, multi-element."""
    spec = ProviderSpec(
        name="minimax",
        type="anthropic_compatible",
        api_key_envs=("PRIMARY_KEY", "BACKUP_KEY"),
        base_url="https://api.minimax.io/anthropic",
    )
    out = to_toml(
        ProvidersConfig(providers={"minimax": spec}, default_model="minimax:M3", agents={})
    )
    assert 'api_key_envs = ["PRIMARY_KEY", "BACKUP_KEY"]' in out


def test_round_trip_list(tmp_path: Path) -> None:
    """serialize via ``to_toml``, reload via ``load_providers_config``; tuple survives."""
    from lies.providers.config import load_providers_config

    path = tmp_path / "providers.toml"
    # Minimal viable TOML for the loader: full agents block, anthropic provider,
    # default_model points at anthropic so cross-reference passes.
    agents_lines = "\n".join(f'{n} = "anthropic:claude-opus-4-7"' for n in AGENT_ROSTER)
    body = (
        'default_model = "anthropic:claude-opus-4-7"\n'
        "\n"
        "[providers.anthropic]\n"
        'type = "anthropic"\n'
        'api_key_envs = ["PRIMARY_KEY", "BACKUP_KEY"]\n'
        "\n"
        "[agents]\n"
        f"{agents_lines}\n"
    )
    path.write_text(body)
    loaded = load_providers_config(path)
    assert loaded is not None
    assert loaded.providers["anthropic"].api_key_envs == ("PRIMARY_KEY", "BACKUP_KEY")
