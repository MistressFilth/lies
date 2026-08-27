"""CLI integration tests for the bootstrap path on ingest/sync/ingest_source."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki

runner = CliRunner()


@pytest.fixture
def wiki(tmp_path: Path) -> Wiki:
    root = tmp_path / "wiki"
    root.mkdir()
    return make_wiki(name="cli-bootstrap", data_root=root)


def test_ingest_with_no_wiki_auto_inits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_WIKI_NAME", "fresh")
    monkeypatch.setattr("lies.cli.xdg.data_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.config_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.cache_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.state_home", lambda: tmp_path)
    monkeypatch.setattr("lies.wiki.wiki.xdg.runtime_dir_for", lambda n: tmp_path / "run" / n)
    with mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync:
        result = runner.invoke(
            app,
            ["ingest", "alpha", "--source", "https://example.com/llms.txt"],
        )
    assert result.exit_code == 0
    assert mock_sync.called


def test_ingest_with_existing_wiki_scaffolds_missing_collection(
    wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIES_WIKI_NAME", wiki.name)
    with mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync:
        result = runner.invoke(
            app,
            ["ingest", "alpha", "--source", "https://example.com/llms.txt", "--name", wiki.name],
        )
    assert result.exit_code == 0
    mock_sync.assert_called_once()
    # YAML persisted
    yaml_path = wiki.collections_dir / "alpha.yaml"
    assert yaml_path.exists()


def test_ingest_with_existing_collection_and_matching_source_runs_sync(
    wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIES_WIKI_NAME", wiki.name)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "alpha.yaml").write_text(
        "name: alpha\npath: /raw/alpha\nsource: https://example.com/llms.txt\n"
        "tags: []\nscraper_cmd: null\ndoc_path: null\nmapper_model: null\n"
        "language: null\nversion: '1'\n"
        "created_at: 2026-01-01T00:00:00+00:00\nupdated_at: 2026-01-01T00:00:00+00:00\n"
        "config: {}\n",
        encoding="utf-8",
    )
    with mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync:
        result = runner.invoke(
            app,
            ["ingest", "alpha", "--source", "https://example.com/llms.txt", "--name", wiki.name],
        )
    assert result.exit_code == 0
    mock_sync.assert_called_once()


def test_ingest_with_existing_collection_and_mismatched_source_errors(
    wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIES_WIKI_NAME", wiki.name)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "alpha.yaml").write_text(
        "name: alpha\npath: /raw/alpha\nsource: https://OLD.example.com/llms.txt\n"
        "tags: []\nscraper_cmd: null\ndoc_path: null\nmapper_model: null\n"
        "language: null\nversion: '1'\n"
        "created_at: 2026-01-01T00:00:00+00:00\nupdated_at: 2026-01-01T00:00:00+00:00\n"
        "config: {}\n",
        encoding="utf-8",
    )
    with mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync:
        result = runner.invoke(
            app,
            [
                "ingest",
                "alpha",
                "--source",
                "https://new.example.com/llms.txt",
                "--name",
                wiki.name,
            ],
        )
    assert result.exit_code == 3
    # typer.echo(..., err=True) writes to stderr; the brief checked
    # ``result.stdout`` but the existing codebase convention
    # (test_cli_flock.py, test_cli_lint_force_repair.py) is to read
    # ``result.stderr`` for error output. Stick with the convention so
    # the assertion matches where the message actually lands.
    err = (result.stderr or "") + (result.stdout or "")
    assert "OLD.example.com" in err
    assert "new.example.com" in err
    assert not mock_sync.called
