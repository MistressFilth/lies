"""Tests for collection-aware CLI subcommands (sync / ingest / reindex / collections)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.etl.heartbeat import Heartbeat
from lies.wiki.wiki import Wiki

runner = CliRunner()


def _use_default_wiki(monkeypatch: pytest.MonkeyPatch) -> str:
    """Switch the CLI to the XDG-default wiki and pre-register its data_root."""
    monkeypatch.setenv("LIES_WIKI_NAME", "default")
    Wiki.data_root_for("default").mkdir(parents=True, exist_ok=True)
    return "default"


def test_sync_help() -> None:
    result = runner.invoke(app, ["sync", "--help"])
    assert result.exit_code == 0
    assert "force" in result.stdout


def test_sync_exits_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_default_wiki(monkeypatch)
    # Simulate a live, non-stale heartbeat (current pid, just started).
    # `acquire_heartbeat` reads it, sees it's busy, and exits with code 2.
    busy = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="other")
    # Patch the read_heartbeat reference used inside the helper
    # (`acquire_heartbeat` resolves it from the sync_helper module's
    # own namespace, so we patch the import path there).
    with mock.patch("lies.etl.sync_helper.read_heartbeat", return_value=busy):
        result = runner.invoke(app, ["sync", "cpython"])
    assert result.exit_code == 2


def test_reindex_reconcile_runs_sync_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lies reindex --reconcile` invokes sync_collection per collection.

    F38: ``lies reindex`` also shells out to ``qmd_reindex`` after the
    reconcile pass. Mock both seams so the test exercises only the
    reconcile path.
    """
    from lies.qmd._models import ReindexResult

    _use_default_wiki(monkeypatch)

    coll_names = ["a", "b", "c"]
    with (
        mock.patch("lies.etl.sync_helper.collection_names", return_value=coll_names),
        mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync,
        mock.patch("lies.cli.WikiLinkResolver.build") as mock_resolver_build,
        mock.patch(
            "lies.qmd.cli.qmd_reindex", return_value=ReindexResult(indexed=True)
        ) as mock_qmd_reindex,
    ):
        result = runner.invoke(app, ["reindex", "--reconcile"])
    assert result.exit_code == 0, result.output
    assert mock_sync.call_count == len(coll_names)
    assert mock_resolver_build.called
    assert mock_qmd_reindex.called


def test_reindex_unknown_flag_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Typer rejects truly unknown flags with a non-zero exit."""
    _use_default_wiki(monkeypatch)

    result = runner.invoke(app, ["reindex", "--no-such-flag"])
    assert result.exit_code != 0
    assert "no such option" in (result.stderr or "").lower()


def test_reindex_no_flags_runs_no_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare ``lies reindex`` shells out to ``qmd_reindex`` (no sync)."""
    from lies.qmd._models import ReindexResult

    _use_default_wiki(monkeypatch)

    with (
        mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync,
        mock.patch(
            "lies.qmd.cli.qmd_reindex", return_value=ReindexResult(indexed=True)
        ) as mock_qmd_reindex,
    ):
        result = runner.invoke(app, ["reindex"])
    assert result.exit_code == 0, result.output
    mock_sync.assert_not_called()
    assert mock_qmd_reindex.called
