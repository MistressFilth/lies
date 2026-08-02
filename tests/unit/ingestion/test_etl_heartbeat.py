import os
import time
from pathlib import Path

import pytest

from lies.etl.heartbeat import (
    MAX_SYNC_AGE_S,
    Heartbeat,
    acquire_create_lock,
    clear_heartbeat,
    heartbeat_is_stale,
    pid_alive,
    read_heartbeat,
    release_create_lock,
    wait_until_free,  # noqa: F401
    write_heartbeat,
)


def test_write_and_read_heartbeat(tmp_path: Path) -> None:
    h = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="cpython")
    write_heartbeat(tmp_path, h)
    assert read_heartbeat(tmp_path) is not None


def test_pid_alive_for_self() -> None:
    assert pid_alive(os.getpid()) is True


def test_pid_alive_for_missing() -> None:
    assert pid_alive(999_999_999) is False


def test_clear_heartbeat(tmp_path: Path) -> None:
    h = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="x")
    write_heartbeat(tmp_path, h)
    clear_heartbeat(tmp_path)
    assert read_heartbeat(tmp_path) is None


def test_stale_when_old(tmp_path: Path) -> None:
    h = Heartbeat(pid=os.getpid(), started_at=time.time() - MAX_SYNC_AGE_S - 10, collection="x")
    assert heartbeat_is_stale(h) is True


def test_acquire_create_lock_winner_and_loser(tmp_path: Path) -> None:
    """Two acquire calls: first wins (fd), second loses (None)."""
    fd1 = acquire_create_lock(tmp_path)
    assert fd1 is not None and fd1 > 0
    fd2 = acquire_create_lock(tmp_path)
    assert fd2 is None
    release_create_lock(tmp_path, fd1)
    # After release, a fresh acquire succeeds.
    fd3 = acquire_create_lock(tmp_path)
    assert fd3 is not None
    release_create_lock(tmp_path, fd3)


def test_acquire_create_lock_release_idempotent(tmp_path: Path) -> None:
    """Releasing with fd=None or non-existent file is a no-op."""
    release_create_lock(tmp_path, None)  # no file to release
    fd = acquire_create_lock(tmp_path)
    assert fd is not None
    release_create_lock(tmp_path, fd)
    # Calling release again should not raise.
    release_create_lock(tmp_path, fd)


def test_acquire_create_lock_recovers_from_orphaned_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale ``.lies/sync.lock.create`` (no fd, old mtime) is recovered.

    Simulates a crashed previous acquirer that left the create-lock file
    behind. The next call must detect the orphan via mtime, unlink it, and
    succeed in claiming the lock. A fresh lock (recent mtime) is left alone.
    """
    (tmp_path / ".lies").mkdir(parents=True)
    lock_path = tmp_path / ".lies" / "sync.lock.create"
    lock_path.touch()

    old_mtime = time.time() - MAX_SYNC_AGE_S - 10
    os.utime(lock_path, (old_mtime, old_mtime))

    fd = acquire_create_lock(tmp_path)
    try:
        assert fd is not None, "stale create lock should be reclaimed"
        # The orphan must be gone — the new fd points at the same inode.
        assert lock_path.exists()
    finally:
        release_create_lock(tmp_path, fd)

    # After release, a fresh lock with a recent mtime must NOT be reclaimed
    # by another acquirer (it is genuinely held).
    fd_a = acquire_create_lock(tmp_path)
    assert fd_a is not None
    fresh_mtime = time.time()
    os.utime(lock_path, (fresh_mtime, fresh_mtime))
    fd_b = acquire_create_lock(tmp_path)
    assert fd_b is None, "recent create lock must not be reclaimed"
    release_create_lock(tmp_path, fd_a)

