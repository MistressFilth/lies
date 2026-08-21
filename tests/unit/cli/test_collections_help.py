"""Dispatch-shape tests for ``lies collections`` subcommand split.

Pre-fix the parent ``lies collections --help`` showed ``{action} <str>``
with no enumeration of valid actions. Post-fix it must surface every
real subcommand (list / show / new / modify / delete) so a user can
discover the surface from ``--help`` alone. Task 5 owns the deeper
parametrized help-text coverage; this file pins the dispatch shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.wiki.wiki import Wiki

runner = CliRunner()


def test_collections_help_lists_every_subcommand() -> None:
    """``lies collections --help`` enumerates list / show / new / modify / delete."""
    result = runner.invoke(app, ["collections", "--help"])
    assert result.exit_code == 0, result.output
    for subcommand in ("list", "show", "new", "modify", "delete"):
        assert subcommand in result.output, f"{subcommand!r} not in help output:\n{result.output}"


def test_collections_list_help_describes_list() -> None:
    """``lies collections list --help`` describes what it does."""
    result = runner.invoke(app, ["collections", "list", "--help"])
    assert result.exit_code == 0, result.output
    assert "List every collection" in result.output


def test_collections_show_help_describes_show() -> None:
    """``lies collections show --help`` describes what it does."""
    result = runner.invoke(app, ["collections", "show", "--help"])
    assert result.exit_code == 0, result.output
    assert "Show a single collection" in result.output


def test_collections_new_help_describes_new() -> None:
    """``lies collections new --help`` describes the wizard."""
    result = runner.invoke(app, ["collections", "new", "--help"])
    assert result.exit_code == 0, result.output
    assert "interactive wizard" in result.output


def test_collections_modify_help_describes_modify() -> None:
    """``lies collections modify --help`` describes mutation."""
    result = runner.invoke(app, ["collections", "modify", "--help"])
    assert result.exit_code == 0, result.output
    assert "Mutate an existing collection" in result.output


def test_collections_delete_help_describes_delete() -> None:
    """``lies collections delete --help`` describes deletion."""
    result = runner.invoke(app, ["collections", "delete", "--help"])
    assert result.exit_code == 0, result.output
    assert "Delete a collection" in result.output


def test_collections_list_returns_same_names_as_pre_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-fix ``lies collections list`` printed each ``.yaml`` stem in
    ``collections_dir``; post-fix must preserve that behavior.
    """
    name = "testwiki"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    wiki = Wiki(
        name=name,
        data_root=xdg.data_home() / "lies" / name,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "col_a.yaml").write_text("", encoding="utf-8")
    (wiki.collections_dir / "col_b.yaml").write_text("", encoding="utf-8")

    result = runner.invoke(app, ["collections", "list", "--name", name])
    assert result.exit_code == 0, result.output
    assert "col_a" in result.output
    assert "col_b" in result.output
