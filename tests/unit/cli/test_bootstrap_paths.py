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


def test_sync_single_collection_bootstrap_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    wiki = make_wiki(name="sync-bootstrap", data_root=wiki_root)
    monkeypatch.setenv("LIES_WIKI_NAME", wiki.name)
    with mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync:
        result = runner.invoke(
            app,
            ["sync", "alpha", "--source", "https://example.com", "--name", wiki.name],
        )
    assert result.exit_code == 0
    mock_sync.assert_called_once()
    assert (wiki.collections_dir / "alpha.yaml").exists()


def test_sync_all_collections_no_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    wiki = make_wiki(name="sync-all", data_root=wiki_root)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "beta.yaml").write_text(
        "name: beta\npath: /raw/beta\nsource: https://b.example.com\n"
        "tags: []\nscraper_cmd: null\ndoc_path: null\nmapper_model: null\n"
        "language: null\nversion: '1'\n"
        "created_at: 2026-01-01T00:00:00+00:00\nupdated_at: 2026-01-01T00:00:00+00:00\n"
        "config: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LIES_WIKI_NAME", wiki.name)
    with (
        mock.patch("lies.etl.sync_helper.collection_names", return_value=["beta"]),
        mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync,
    ):
        result = runner.invoke(app, ["sync", "--name", wiki.name])
    assert result.exit_code == 0
    mock_sync.assert_called_once()
    # No bootstrap happened for an unrelated collection name
    assert not (wiki.collections_dir / "alpha.yaml").exists()


def test_sync_existing_collection_mismatched_source_errors(
    wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIES_WIKI_NAME", wiki.name)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "alpha.yaml").write_text(
        "name: alpha\npath: /raw/alpha\nsource: https://OLD.example.com\n"
        "tags: []\nscraper_cmd: null\ndoc_path: null\nmapper_model: null\n"
        "language: null\nversion: '1'\n"
        "created_at: 2026-01-01T00:00:00+00:00\nupdated_at: 2026-01-01T00:00:00+00:00\n"
        "config: {}\n",
        encoding="utf-8",
    )
    with mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync:
        result = runner.invoke(
            app,
            ["sync", "alpha", "--source", "https://new.example.com", "--name", wiki.name],
        )
    assert result.exit_code == 3
    err = (result.stderr or "") + (result.stdout or "")
    assert "OLD.example.com" in err
    assert "new.example.com" in err
    assert not mock_sync.called


# ---------------------------------------------------------------------------
# Task 5: ingest_source requires --collection (hard cutover) and bootstraps
# the YAML on missing, then routes through Orchestrator.run_ingest.
# ---------------------------------------------------------------------------


def test_ingest_source_without_collection_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """``lies ingest-source URL`` (no --collection) must be rejected.

    Typer surfaces the missing-required-option error to stderr; the
    message includes the option name ``--collection``.
    """
    monkeypatch.setenv("LIES_WIKI_NAME", "any")
    result = runner.invoke(
        app,
        ["ingest-source", "https://example.com/llms.txt"],
    )
    assert result.exit_code != 0
    # Task 3 convention: combine stderr + stdout so the assertion is robust
    # to which stream typer writes the missing-arg error to.
    err = (result.stderr or "") + (result.stdout or "")
    assert "collection" in err.lower()


def test_ingest_source_with_collection_bootstraps_and_calls_run_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    wiki = make_wiki(name="ingest-src-bootstrap", data_root=wiki_root)
    monkeypatch.setenv("LIES_WIKI_NAME", wiki.name)
    fake_orchestrator = mock.MagicMock()
    fake_orchestrator.run_ingest.return_value = "ok"
    # Patch the consumer-side lazy proxy ``lies.cli.ingestion.Orchestrator``
    # (set up via the module-level ``__getattr__`` in ingestion.py) so the
    # ``Orchestrator(wiki)`` call inside ``ingest_source`` returns our
    # mock without instantiating the real heavy stack.
    with mock.patch("lies.cli.ingestion.Orchestrator", return_value=fake_orchestrator):
        result = runner.invoke(
            app,
            [
                "ingest-source",
                "https://example.com/llms.txt",
                "--collection",
                "alpha",
                "--name",
                wiki.name,
            ],
        )
    assert result.exit_code == 0
    fake_orchestrator.run_ingest.assert_called_once_with(
        "https://example.com/llms.txt", no_llm=False
    )
    assert (wiki.collections_dir / "alpha.yaml").exists()


def test_ingest_source_with_collection_mismatch_errors(
    wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing YAML with a different source raises CollectionMismatch (exit 3)."""
    monkeypatch.setenv("LIES_WIKI_NAME", wiki.name)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "alpha.yaml").write_text(
        "name: alpha\npath: /raw/alpha\nsource: https://OLD.example.com\n"
        "tags: []\nscraper_cmd: null\ndoc_path: null\nmapper_model: null\n"
        "language: null\nversion: '1'\n"
        "created_at: 2026-01-01T00:00:00+00:00\nupdated_at: 2026-01-01T00:00:00+00:00\n"
        "config: {}\n",
        encoding="utf-8",
    )
    fake_orchestrator = mock.MagicMock()
    with mock.patch("lies.cli.ingestion.Orchestrator", return_value=fake_orchestrator):
        result = runner.invoke(
            app,
            [
                "ingest-source",
                "https://new.example.com/llms.txt",
                "--collection",
                "alpha",
                "--name",
                wiki.name,
            ],
        )
    assert result.exit_code == 3
    err = (result.stderr or "") + (result.stdout or "")
    assert "OLD.example.com" in err
    assert "new.example.com" in err
    assert not fake_orchestrator.run_ingest.called


# ---------------------------------------------------------------------------
# Task 7: --wizard flag raises WizardRequiresTTY (exit 4) without a TTY.
# ---------------------------------------------------------------------------


def test_ingest_wizard_requires_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``lies ingest alpha --source ... --wizard`` without a TTY exits 4.

    The CLI catches ``WizardRequiresTTY`` and surfaces it as exit code 4
    (distinct from the existing 3/5 buckets). The check fires before any
    ``sync_collection`` call, so the bootstrap path short-circuits cleanly.
    """
    monkeypatch.setenv("LIES_WIKI_NAME", "wiz")
    monkeypatch.setattr("lies.cli.xdg.data_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.config_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.cache_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.state_home", lambda: tmp_path)
    monkeypatch.setattr("lies.wiki.wiki.xdg.runtime_dir_for", lambda n: tmp_path / "run" / n)
    monkeypatch.setattr("lies.collections.bootstrap.sys.stdin.isatty", lambda: False)
    with mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync:
        result = runner.invoke(
            app,
            [
                "ingest",
                "alpha",
                "--source",
                "https://example.com",
                "--wizard",
            ],
        )
    # WizardRequiresTTY propagates as an unhandled exception in the body;
    # the CLI translates it to exit code 4.
    assert result.exit_code == 4
    assert not mock_sync.called
