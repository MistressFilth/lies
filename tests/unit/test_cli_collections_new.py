"""Tests for `lies collections new --prompt` author CLI verb."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from lies.agents.collection_author import AuthorProposal
from lies.cli import app

runner = CliRunner()


def _make_proposal(tmp_path: Path) -> AuthorProposal:
    return AuthorProposal(
        collection={
            "name": "htmx",
            "path": str(tmp_path / "raw" / "htmx"),
            "source": "https://github.com/bigskysoftware/htmx",
            "tags": ["docs"],
            "scraper_cmd": None,
            "doc_path": None,
            "mapper_model": None,
            "language": "en",
            "version": "1.0.0",
            "created_at": "2026-08-01T00:00:00+00:00",
            "updated_at": "2026-08-01T00:00:00+00:00",
            "config": {"sphinx_excludes": ["_templates/**"]},
        },
        rationale="curated htmx docs",
    )


def test_collections_new_prints_yaml_without_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --apply, the YAML is printed to stdout and the file is NOT created."""
    monkeypatch.setenv("LIES_WIKI_ROOT", str(tmp_path))
    proposal = _make_proposal(tmp_path)
    fake_agent = mock.Mock()
    fake_agent.run_sync.return_value = mock.Mock(
        output=proposal,
        new_messages=list,
    )
    with (
        mock.patch(
            "lies.agents.collection_author.collection_author_agent",
            return_value=fake_agent,
        ),
        mock.patch("lies.cli.pick_scraper") as m_pick,
    ):
        m_pick.return_value.emit_manifest.return_value = tmp_path / "manifest.json"
        result = runner.invoke(
            app,
            [
                "collections",
                "new",
                "htmx",
                "--source",
                "https://github.com/bigskysoftware/htmx",
                "--prompt",
                "the htmx docs",
            ],
        )
    assert result.exit_code == 0, result.stdout
    assert "name: htmx" in result.stdout
    assert not (tmp_path / ".lies" / "collections" / "htmx.yaml").exists()


def test_collections_new_writes_yaml_with_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --apply, the YAML file is written to .lies/collections/<name>.yaml."""
    monkeypatch.setenv("LIES_WIKI_ROOT", str(tmp_path))
    proposal = _make_proposal(tmp_path)
    fake_agent = mock.Mock()
    fake_agent.run_sync.return_value = mock.Mock(
        output=proposal,
        new_messages=list,
    )
    with (
        mock.patch(
            "lies.agents.collection_author.collection_author_agent",
            return_value=fake_agent,
        ),
        mock.patch("lies.cli.pick_scraper") as m_pick,
    ):
        m_pick.return_value.emit_manifest.return_value = tmp_path / "manifest.json"
        result = runner.invoke(
            app,
            [
                "collections",
                "new",
                "htmx",
                "--source",
                "https://github.com/bigskysoftware/htmx",
                "--prompt",
                "the htmx docs",
                "--apply",
            ],
        )
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / ".lies" / "collections" / "htmx.yaml").exists()
