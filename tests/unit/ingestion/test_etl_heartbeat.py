import os
import time
from pathlib import Path

import pytest

from lies import xdg
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
from lies.wiki.wiki import Wiki


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A Wiki with all five XDG roots under ``tmp_path`` so tests are hermetic."""
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    name = "test"
    wiki = Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    wiki.runtime_root.mkdir(parents=True, exist_ok=True)
    return wiki


def test_write_and_read_heartbeat(wiki: Wiki) -> None:
    h = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="cpython")
    write_heartbeat(wiki, h)
    assert read_heartbeat(wiki) is not None


def test_pid_alive_for_self() -> None:
    assert pid_alive(os.getpid()) is True


def test_pid_alive_for_missing() -> None:
    assert pid_alive(999_999_999) is False


def test_clear_heartbeat(wiki: Wiki) -> None:
    h = Heartbeat(pid=os.getpid(), started_at=time.time(), collection="x")
    write_heartbeat(wiki, h)
    clear_heartbeat(wiki)
    assert read_heartbeat(wiki) is None


def test_stale_when_old() -> None:
    h = Heartbeat(pid=os.getpid(), started_at=time.time() - MAX_SYNC_AGE_S - 10, collection="x")
    assert heartbeat_is_stale(h) is True


def test_acquire_create_lock_winner_and_loser(wiki: Wiki) -> None:
    """Two acquire calls: first wins (fd), second loses (None)."""
    fd1 = acquire_create_lock(wiki)
    assert fd1 is not None and fd1 > 0
    fd2 = acquire_create_lock(wiki)
    assert fd2 is None
    release_create_lock(wiki, fd1)
    # After release, a fresh acquire succeeds.
    fd3 = acquire_create_lock(wiki)
    assert fd3 is not None
    release_create_lock(wiki, fd3)


def test_acquire_create_lock_release_idempotent(wiki: Wiki) -> None:
    """Releasing with fd=None or non-existent file is a no-op."""
    release_create_lock(wiki, None)  # no file to release
    fd = acquire_create_lock(wiki)
    assert fd is not None
    release_create_lock(wiki, fd)
    # Calling release again should not raise.
    release_create_lock(wiki, fd)


def test_acquire_create_lock_recovers_from_orphaned_lock(
    wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale ``sync.lock.create`` (no fd, old mtime) is recovered.

    Simulates a crashed previous acquirer that left the create-lock file
    behind. The next call must detect the orphan via mtime, unlink it, and
    succeed in claiming the lock. A fresh lock (recent mtime) is left alone.
    """
    lock_path = wiki.sync_create_lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()

    old_mtime = time.time() - MAX_SYNC_AGE_S - 10
    os.utime(lock_path, (old_mtime, old_mtime))

    fd = acquire_create_lock(wiki)
    try:
        assert fd is not None, "stale create lock should be reclaimed"
        # The orphan must be gone — the new fd points at the same inode.
        assert lock_path.exists()
    finally:
        release_create_lock(wiki, fd)

    # After release, a fresh lock with a recent mtime must NOT be reclaimed
    # by another acquirer (it is genuinely held).
    fd_a = acquire_create_lock(wiki)
    assert fd_a is not None
    fresh_mtime = time.time()
    os.utime(lock_path, (fresh_mtime, fresh_mtime))
    fd_b = acquire_create_lock(wiki)
    assert fd_b is None, "recent create lock must not be reclaimed"
    release_create_lock(wiki, fd_a)
