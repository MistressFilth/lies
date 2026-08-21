"""Tests that ``lies providers list`` enumerates configured providers.

Adds coverage for the new subcommand introduced in the CLI help-revamp:
the parent ``lies providers --help`` previously surfaced only the wizard
+ companion operators; this file pins the new ``list`` subcommand's
shape (help text + table output + --json shape).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()


@pytest.fixture
def seeded_providers_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a providers.toml with two providers + agent assignments.

    The fixture intentionally omits ``type`` on each provider: the
    ``providers list`` subcommand reads the file via raw ``tomllib``
    rather than ``load_providers_config`` (no full validation), so the
    listing path works on partial / hand-edited files without forcing
    the operator to satisfy the strict loader schema first.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    providers_dir = tmp_path / "config" / "lies"
    providers_dir.mkdir(parents=True)
    (providers_dir / "providers.toml").write_text(
        textwrap.dedent(
            """\
            default_model = "minimax:default"
            [providers.minimax]
            api_key_env = "MINIMAX_API_KEY"

            [providers.local-ollama]
            api_key_env = "OLLAMA_API_KEY"

            [agents]
            orchestrator = "minimax:claude-opus"
            """
        ),
        encoding="utf-8",
    )
    return providers_dir


def test_providers_list_help_describes_list(seeded_providers_toml) -> None:
    """``lies providers list --help`` describes what the subcommand does."""
    result = runner.invoke(app, ["providers", "list", "--help"])
    assert result.exit_code == 0, result.output
    assert "List every provider" in result.output


def test_providers_list_prints_table(seeded_providers_toml) -> None:
    """``lies providers list`` renders a table containing every configured provider id."""
    result = runner.invoke(app, ["providers", "list"])
    assert result.exit_code == 0, result.output
    assert "minimax" in result.output
    assert "local-ollama" in result.output


def test_providers_list_json(seeded_providers_toml) -> None:
    """``lies providers list --json`` emits a JSON array whose ids match the TOML providers."""
    result = runner.invoke(app, ["providers", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "minimax" in {p["id"] for p in payload}
    assert "local-ollama" in {p["id"] for p in payload}
