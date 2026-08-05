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


def test_reindex_embed_warns_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`lies reindex --embed` exits 0 but emits a stderr warning."""
    _use_default_wiki(monkeypatch)

    result = runner.invoke(app, ["reindex", "--embed"])
    assert result.exit_code == 0
    assert "--embed is a no-op" in (result.stderr or "")


def test_reindex_cleanup_warns_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`lies reindex --cleanup` exits 0 but emits a stderr warning."""
    _use_default_wiki(monkeypatch)

    result = runner.invoke(app, ["reindex", "--cleanup"])
    assert result.exit_code == 0
    assert "--cleanup is a no-op" in (result.stderr or "")


def test_reindex_reconcile_does_not_warn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`lies reindex --reconcile` does not emit the no-op warning."""
    _use_default_wiki(monkeypatch)

    # No collections to sync; just verify the no-op warning does not appear.
    with (
        mock.patch("lies.qmd.cli.qmd_embed") as mock_embed,
        mock.patch("lies.qmd.cli.qmd_cleanup") as mock_cleanup,
        mock.patch("lies.etl.sync_helper.sync_collection"),
    ):
        result = runner.invoke(app, ["reindex", "--reconcile"])
    assert result.exit_code == 0
    mock_embed.assert_not_called()
    mock_cleanup.assert_not_called()
    assert "--embed is a no-op" not in (result.stderr or "")
    assert "--cleanup is a no-op" not in (result.stderr or "")
