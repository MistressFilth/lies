from __future__ import annotations

import json
import os
import time
from pathlib import Path

from lies.utils import exclusive
from lies.utils.exclusive import (
    MAX_FLOCK_AGE_S_DEFAULT,
    acquire_create_lock,
    release_create_lock,
)


def test_acquire_returns_fd_when_free(tmp_path: Path) -> None:
    lock = tmp_path / "sub" / "thing.lock.create"
    fd = acquire_create_lock(lock, max_age_s=60.0)
    assert fd is not None
    assert lock.exists()
    release_create_lock(lock, fd)


def test_second_acquire_is_none(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    first = acquire_create_lock(lock, max_age_s=60.0)
    second = acquire_create_lock(lock, max_age_s=60.0)
    assert first is not None
    assert second is None
    release_create_lock(lock, first)


def test_release_unlinks_and_reallows(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    fd = acquire_create_lock(lock, max_age_s=60.0)
    release_create_lock(lock, fd)
    assert not lock.exists()
    again = acquire_create_lock(lock, max_age_s=60.0)
    assert again is not None
    release_create_lock(lock, again)


def test_orphan_reclaimed_past_window(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    fd = acquire_create_lock(lock, max_age_s=60.0)
    assert fd is not None
    old = time.time() - 120
    os.utime(lock, (old, old))
    reclaimed = acquire_create_lock(lock, max_age_s=60.0)
    assert reclaimed is not None
    release_create_lock(lock, reclaimed)


def test_orphan_not_reclaimed_inside_window(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    fd = acquire_create_lock(lock, max_age_s=3600.0)
    old = time.time() - 120
    os.utime(lock, (old, old))
    assert acquire_create_lock(lock, max_age_s=3600.0) is None
    release_create_lock(lock, fd)


def test_release_tolerates_none_fd_and_missing_file(tmp_path: Path) -> None:
    release_create_lock(tmp_path / "absent", None)


def test_ensure_gitignored_removed() -> None:
    """The gitignore guard has been replaced by XDG runtime paths."""
    assert not hasattr(exclusive, "ensure_gitignored")


def test_acquire_create_lock_reaps_dead_pid_and_reacquires(
    tmp_path: Path,
) -> None:
    """A pre-existing flock whose stored PID is dead must be reaped on
    the next acquire, and the second acquire must succeed."""
    lock_create = tmp_path / "memory.lock.create"
    pid_path = tmp_path / "memory.pid"
    state_json = tmp_path / "memory.state.json"

    # First holder: simulate a dead-PID by writing a known-dead pid.
    lock_create.touch()
    pid_path.write_text("999999", encoding="utf-8")
    state_json.write_text(
        json.dumps({"pid": 999999, "started_at": 0.0, "scope": "test"}),
        encoding="utf-8",
    )

    # Pretend the stored PID is dead via monkeypatched pid_alive_fn.
    fd = acquire_create_lock(
        lock_create,
        max_age_s=MAX_FLOCK_AGE_S_DEFAULT,
        pid_path=pid_path,
        state_json_path=state_json,
        pid_alive_fn=lambda _: False,
    )
    assert fd is not None, "expected reap-then-reacquire to succeed"
    release_create_lock(
        lock_create,
        fd,
        pid_path=pid_path,
        state_json_path=state_json,
    )
    assert not lock_create.exists()
    assert not pid_path.exists()
    assert not state_json.exists()
