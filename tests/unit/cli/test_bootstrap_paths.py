"""CLI integration tests for the bootstrap path on sync + ``ingest-source`` stub."""

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


def test_ingest_source_stub_errors_with_deprecation_message() -> None:
    """``ingest-source`` is a one-minor-version deprecation stub.

    Any invocation exits non-zero with a stderr message steering the
    operator to ``lies ingest --source``.
    """
    result = runner.invoke(
        app,
        ["ingest-source", "https://example.com/llms.txt", "--collection", "alpha"],
    )
    assert result.exit_code == 2
    err = (result.stderr or "") + (result.stdout or "")
    assert "ingest --source" in err


def test_sync_single_collection_bootstrap_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    wiki = make_wiki(name="sync-bootstrap", data_root=wiki_root)
    monkeypatch.setenv("LIES_WIKI_NAME", wiki.name)
    # Task 11 retarget: ``sync_collection`` now returns a
    # ``BatchIngestResult``. The CLI exits 1 when ``errors`` is
    # non-zero. Mock the helper to return a clean result so the
    # bootstrap-only path can be tested.
    from lies.library.ingest import BatchIngestResult

    with mock.patch(
        "lies.etl.sync_helper.sync_collection",
        return_value=BatchIngestResult(),
    ) as mock_sync:
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
    from lies.library.ingest import BatchIngestResult

    with (
        mock.patch("lies.etl.sync_helper.collection_names", return_value=["beta"]),
        mock.patch(
            "lies.etl.sync_helper.sync_collection",
            return_value=BatchIngestResult(),
        ) as mock_sync,
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
