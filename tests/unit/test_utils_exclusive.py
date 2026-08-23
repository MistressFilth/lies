from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from lies.utils import exclusive
from lies.utils.exclusive import (
    MAX_FLOCK_AGE_S,
    acquire_create_lock,
    release_create_lock,
)


def test_acquire_returns_fd_when_free(tmp_path: Path) -> None:
    lock = tmp_path / "sub" / "thing.lock.create"
    result = acquire_create_lock(lock, max_age_s=60.0)
    assert result is not None
    assert result.fd > 0
    assert result.status == "acquired"
    assert lock.exists()
    release_create_lock(lock, result.fd)


def test_second_acquire_is_none(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    first = acquire_create_lock(lock, max_age_s=60.0)
    second = acquire_create_lock(lock, max_age_s=60.0)
    assert first is not None
    # Legacy path: no envelope -> contention is still ``None``.
    assert second is None
    release_create_lock(lock, first.fd)


def test_release_unlinks_and_reallows(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    result = acquire_create_lock(lock, max_age_s=60.0)
    assert result is not None
    release_create_lock(lock, result.fd)
    assert not lock.exists()
    again = acquire_create_lock(lock, max_age_s=60.0)
    assert again is not None
    release_create_lock(lock, again.fd)


def test_orphan_reclaimed_past_window(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    result = acquire_create_lock(lock, max_age_s=60.0)
    assert result is not None
    old = time.time() - 120
    os.utime(lock, (old, old))
    reclaimed = acquire_create_lock(lock, max_age_s=60.0)
    assert reclaimed is not None
    release_create_lock(lock, reclaimed.fd)


def test_orphan_not_reclaimed_inside_window(tmp_path: Path) -> None:
    lock = tmp_path / "thing.lock.create"
    result = acquire_create_lock(lock, max_age_s=3600.0)
    assert result is not None
    old = time.time() - 120
    os.utime(lock, (old, old))
    assert acquire_create_lock(lock, max_age_s=3600.0) is None
    release_create_lock(lock, result.fd)


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
    result = acquire_create_lock(
        lock_create,
        max_age_s=MAX_FLOCK_AGE_S,
        pid_path=pid_path,
        state_json_path=state_json,
        pid_alive_fn=lambda _: False,
    )
    assert result is not None, "expected reap-then-reacquire to succeed"
    assert result.fd > 0, "AcquireResult must carry a valid fd"
    assert result.status == "dead_reaped"
    release_create_lock(
        lock_create,
        result.fd,
        pid_path=pid_path,
        state_json_path=state_json,
    )
    assert not lock_create.exists()
    assert not pid_path.exists()
    assert not state_json.exists()


def test_pid_alive_classifier_handles_process_lookup_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """ESRCH from os.kill(pid, 0) must be classified as dead -> reap + retry."""
    lock_create = tmp_path / "memory.lock.create"
    pid_path = tmp_path / "memory.pid"
    state_json = tmp_path / "memory.state.json"

    lock_create.touch()
    pid_path.write_text("999999", encoding="utf-8")
    state_json.write_text(
        json.dumps({"pid": 999999, "started_at": 0.0, "scope": "test"}),
        encoding="utf-8",
    )

    with patch("os.kill", side_effect=ProcessLookupError("ESRCH")):
        result = acquire_create_lock(
            lock_create,
            max_age_s=MAX_FLOCK_AGE_S,
            pid_path=pid_path,
            state_json_path=state_json,
        )

    assert result is not None, "ESRCH must trigger reap + retry"
    assert result.status == "dead_reaped"
    release_create_lock(
        lock_create,
        result.fd,
        pid_path=pid_path,
        state_json_path=state_json,
    )


def test_pid_alive_classifier_handles_permission_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """EPERM from os.kill(pid, 0) must be classified as busy -> no reap."""
    lock_create = tmp_path / "memory.lock.create"
    pid_path = tmp_path / "memory.pid"
    state_json = tmp_path / "memory.state.json"

    lock_create.touch()
    pid_path.write_text("999999", encoding="utf-8")
    # Fresh heartbeat so the wall-clock branch does not also trigger reap.
    state_json.write_text(
        json.dumps({"pid": 999999, "started_at": time.time(), "scope": "test"}),
        encoding="utf-8",
    )

    caplog.set_level(logging.WARNING, logger="lies.utils.exclusive")
    with patch("os.kill", side_effect=PermissionError("EPERM")):
        result = acquire_create_lock(
            lock_create,
            max_age_s=MAX_FLOCK_AGE_S,
            pid_path=pid_path,
            state_json_path=state_json,
        )

    # Envelope-aware caller: busy path now returns a populated
    # AcquireResult instead of ``None``; ``fd`` is the sentinel ``-1``.
    assert result is not None, "EPERM must yield a busy AcquireResult"
    assert result.fd == -1
    assert result.status == "busy"
    assert result.holder_pid == 999999
    assert lock_create.exists(), "lock file must remain after busy path"
    assert pid_path.exists(), "pid file must remain after busy path"
    assert "raised non-ESRCH/EPERM" not in caplog.text, (
        "EPERM path must NOT emit the WARN log reserved for unknown OSError"
    )


def test_pid_alive_classifier_handles_generic_oserror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unknown OSError from os.kill(pid, 0) must be classified as busy + WARN."""
    lock_create = tmp_path / "memory.lock.create"
    pid_path = tmp_path / "memory.pid"
    state_json = tmp_path / "memory.state.json"

    lock_create.touch()
    pid_path.write_text("999999", encoding="utf-8")
    # Fresh heartbeat so the wall-clock branch does not also trigger reap.
    state_json.write_text(
        json.dumps({"pid": 999999, "started_at": time.time(), "scope": "test"}),
        encoding="utf-8",
    )

    caplog.set_level(logging.WARNING, logger="lies.utils.exclusive")
    with patch("os.kill", side_effect=OSError("EACCES: foo")):
        result = acquire_create_lock(
            lock_create,
            max_age_s=MAX_FLOCK_AGE_S,
            pid_path=pid_path,
            state_json_path=state_json,
        )

    assert result is not None, "unknown OSError must yield a busy AcquireResult"
    assert result.fd == -1
    assert result.status == "busy"
    assert result.holder_pid == 999999
    assert lock_create.exists(), "lock file must remain after busy path"
    assert pid_path.exists(), "pid file must remain after busy path"
    assert "raised non-ESRCH/EPERM" in caplog.text, "unknown OSError path must emit the WARN log"
