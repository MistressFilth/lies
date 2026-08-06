"""`lies config` lists one model line per agent in AGENT_ROSTER."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.providers.agents import AGENT_ROSTER


def _write_providers_toml(providers_path: Path) -> None:
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
    providers_path.write_text(dedent(body).lstrip() + "\n" + agents_block + "\n")


def test_config_prints_per_agent_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_WIKI_NAME", "default")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")

    # Init the wiki.
    runner = CliRunner()
    init = runner.invoke(app, ["init", "default"])
    assert init.exit_code == 0, init.output

    # Now write providers.toml into the wiki's user-level providers_path.
    from lies.wiki.wiki import Wiki

    providers_path = Wiki.require("default").providers_path
    _write_providers_toml(providers_path)

    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0, result.output
    assert "wiki: default" in result.output
    assert "model: anthropic:claude-opus-4-7" in result.output
    assert "agent models:" in result.output
    for name in AGENT_ROSTER:
        assert name in result.output
    assert "minimax:MiniMax-M3" in result.output


def test_config_handles_missing_providers_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_WIKI_NAME", "default")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    runner = CliRunner()
    init = runner.invoke(app, ["init", "default"])
    assert init.exit_code == 0, init.output

    # No providers.toml — every agent should still resolve.
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0, result.output
    assert "(no providers.toml" in result.output
    assert "agent models: (none configured)" in result.output
