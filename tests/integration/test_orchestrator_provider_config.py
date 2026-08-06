"""Orchestrator constructs all agents with the new providers.toml resolver path."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from lies.providers import AGENT_ROSTER
from lies.wiki.wiki import Wiki


def _write_providers_toml(config_dir: Path) -> Path:
    providers = config_dir / "providers.toml"
    body = """
        default_model = "anthropic:claude-opus-4-7"

        [providers.anthropic]
        type = "anthropic"
        api_key_env = "ANTHROPIC_API_KEY"

        [providers.minimax]
        type = "anthropic_compatible"
        base_url = "https://api.minimax.io/anthropic"
        api_key_env = "MINIMAX_API_KEY"

        [agents]
    """
    minimax_agents = {"source_reader", "page_writer"}
    agents_block = "\n".join(
        f'{n} = "minimax:MiniMax-M3"'
        if n in minimax_agents
        else f'{n} = "anthropic:claude-opus-4-7"'
        for n in AGENT_ROSTER
    )
    providers.write_text(dedent(body).lstrip() + "\n" + agents_block + "\n")
    return providers


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_WIKI_NAME", "default")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    data_root = Wiki.data_root_for("default")
    data_root.mkdir(parents=True)
    (data_root / "raw").mkdir()
    (data_root / "wiki").mkdir()
    config_root = Wiki.require("default").config_root
    config_root.mkdir(parents=True, exist_ok=True)
    providers_dir = Wiki.require("default").providers_path.parent
    providers_dir.mkdir(parents=True, exist_ok=True)
    _write_providers_toml(providers_dir)
    return Wiki.require("default")


def test_orchestrator_constructs_with_providers_toml(wiki: Wiki) -> None:
    from lies.orchestrator import Orchestrator

    orch = Orchestrator(wiki=wiki)
    # Orchestrator exposes models dict; every roster entry resolves.
    for name in AGENT_ROSTER:
        assert name in orch.models


def test_resolves_anthropic_string_for_built_in(wiki: Wiki) -> None:
    from lies.orchestrator import Orchestrator

    orch = Orchestrator(wiki=wiki)
    assert orch.models["orchestrator"] == "anthropic:claude-opus-4-7"


def test_resolves_anthropic_model_for_custom(wiki: Wiki) -> None:
    from pydantic_ai.models.anthropic import AnthropicModel

    from lies.orchestrator import Orchestrator

    orch = Orchestrator(wiki=wiki)
    assert isinstance(orch.models["source_reader"], AnthropicModel)


def test_linter_env_override_beats_toml(wiki: Wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    """``LIES_LINTER_MODEL`` wins over the ``linter =`` value in providers.toml."""
    from lies.orchestrator import Orchestrator

    # The fixture writes `linter = "anthropic:claude-opus-4-7"`; this
    # env override should win for the linter entry only.
    monkeypatch.setenv("LIES_LINTER_MODEL", "anthropic:claude-haiku-4-5")
    orch = Orchestrator(wiki=wiki)
    assert orch.models["linter"] == "anthropic:claude-haiku-4-5"
