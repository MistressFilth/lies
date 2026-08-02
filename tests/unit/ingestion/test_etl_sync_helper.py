"""Tests for the sync_helper orchestration module."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest import mock

from lies.etl.heartbeat import Heartbeat
from lies.etl.sync_helper import acquire_heartbeat, release_heartbeat


def test_acquire_heartbeat_writes_and_releases(tmp_path: Path) -> None:
    """acquire_heartbeat writes the heartbeat; release clears it."""
    (tmp_path / ".lies").mkdir(parents=True)
    hb = acquire_heartbeat(tmp_path, wait=False, fail_busy=True)
    assert hb is not None
    assert hb.pid == os.getpid()
    # The sibling lock file must exist while we hold the heartbeat.
    assert (tmp_path / ".lies" / "sync.lock.create").exists()
    release_heartbeat(tmp_path)
    assert not (tmp_path / ".lies" / "sync.lock.create").exists()
    assert not (tmp_path / ".lies" / "sync.lock").exists()


def test_acquire_heartbeat_returns_none_when_busy(tmp_path: Path) -> None:
    """Two concurrent acquirers cannot both win the create lock."""
    (tmp_path / ".lies").mkdir(parents=True)
    busy = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="other")
    with mock.patch("lies.etl.sync_helper.read_heartbeat", return_value=busy):
        hb = acquire_heartbeat(tmp_path, wait=False, fail_busy=True)
    assert hb is None


def test_acquire_heartbeat_atomic_under_concurrent_acquire(tmp_path: Path) -> None:
    """Two concurrent acquire_create_lock calls cannot both succeed.

    This exercises the OS-level O_EXCL guarantee that backs the
    TOCTOU-safe acquire path. The first wins the create; the second
    gets FileExistsError → None.
    """
    from lies.etl.heartbeat import acquire_create_lock, release_create_lock

    fd1 = acquire_create_lock(tmp_path)
    fd2 = acquire_create_lock(tmp_path)
    assert fd1 is not None
    assert fd2 is None
    release_create_lock(tmp_path, fd1)


def test_release_heartbeat_no_held_lock(tmp_path: Path) -> None:
    """release_heartbeat is safe even if the lock file is absent."""
    (tmp_path / ".lies").mkdir(parents=True)
    # No acquire first; release should be a no-op (no raise).
    release_heartbeat(tmp_path)
