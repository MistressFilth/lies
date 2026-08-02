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

runner = CliRunner()


def test_sync_help() -> None:
    result = runner.invoke(app, ["sync", "--help"])
    assert result.exit_code == 0
    assert "force" in result.stdout


def test_sync_exits_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIES_WIKI_ROOT", str(tmp_path))
    (tmp_path / ".lies").mkdir()
    # Simulate a live, non-stale heartbeat (current pid, just started).
    # `acquire_heartbeat` reads it, sees it's busy, and exits with code 2.
    busy = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="other")
    # Patch the read_heartbeat reference used inside the helper
    # (`acquire_heartbeat` resolves it from the sync_helper module's
    # own namespace, so we patch the import path there).
    with mock.patch("lies.etl.sync_helper.read_heartbeat", return_value=busy):
        result = runner.invoke(app, ["sync", "cpython"])
    assert result.exit_code == 2
