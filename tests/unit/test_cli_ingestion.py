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
    """`lies reindex --reconcile` invokes sync_collection per collection."""
    _use_default_wiki(monkeypatch)

    coll_names = ["a", "b", "c"]
    with (
        mock.patch("lies.etl.sync_helper.collection_names", return_value=coll_names),
        mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync,
    ):
        result = runner.invoke(app, ["reindex", "--reconcile"])
    assert result.exit_code == 0
    assert mock_sync.call_count == len(coll_names)
    assert "--embed is a no-op" not in (result.stderr or "")
    assert "--cleanup is a no-op" not in (result.stderr or "")


def test_reindex_unknown_flag_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`lies reindex --embed` exits non-zero; typer rejects unknown flag."""
    _use_default_wiki(monkeypatch)

    result = runner.invoke(app, ["reindex", "--embed"])
    assert result.exit_code != 0
    assert "no such option" in (result.stderr or "").lower()


def test_reindex_no_flags_runs_no_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare `lies reindex` is a no-op; sync_collection is not called."""
    _use_default_wiki(monkeypatch)

    with mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync:
        result = runner.invoke(app, ["reindex"])
    assert result.exit_code == 0
    mock_sync.assert_not_called()
    assert "--embed is a no-op" not in (result.stderr or "")
    assert "--cleanup is a no-op" not in (result.stderr or "")
