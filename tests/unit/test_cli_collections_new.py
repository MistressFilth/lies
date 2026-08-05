"""Tests for `lies collections new --prompt` author CLI verb."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.agents.collection_author import AuthorProposal
from lies.cli import app
from lies.wiki.wiki import Wiki

runner = CliRunner()


def _make_proposal(name: str, tmp_path: Path) -> AuthorProposal:
    return AuthorProposal(
        collection={
            "name": name,
            "path": str(tmp_path / "raw" / name),
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


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    name = "new"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
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
    wiki.scratch_dir.mkdir(parents=True, exist_ok=True)
    return wiki


def test_collections_new_prints_yaml_without_apply(wiki: Wiki) -> None:
    """Without --apply, the YAML is printed to stdout and the file is NOT created."""
    proposal = _make_proposal("htmx", wiki.data_root)
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
        m_pick.return_value.emit_manifest.return_value = wiki.scratch_dir / "manifest.json"
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
    assert not (wiki.collections_dir / "htmx.yaml").exists()


def test_collections_new_writes_yaml_with_apply(wiki: Wiki) -> None:
    """With --apply, the YAML file is written to the XDG collections_dir."""
    proposal = _make_proposal("htmx", wiki.data_root)
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
        m_pick.return_value.emit_manifest.return_value = wiki.scratch_dir / "manifest.json"
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
    assert (wiki.collections_dir / "htmx.yaml").exists()
