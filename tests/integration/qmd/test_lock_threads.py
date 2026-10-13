"""The qmd lock is reentrant on one thread and still exclusive across threads."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


def test_a_second_thread_still_contends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second thread must still serialize behind the first."""
    from lies.qmd.lock import with_qmd_lock

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "threads.lock"))
    holding = threading.Event()
    release = threading.Event()
    second_ran = threading.Event()

    @with_qmd_lock(timeout_s=5.0)
    def holder() -> None:
        holding.set()
        release.wait(5.0)

    @with_qmd_lock(timeout_s=0.12)
    def second() -> None:
        second_ran.set()

    t1 = threading.Thread(target=holder)
    t1.start()
    try:
        assert holding.wait(5.0), "holder never acquired"
        t2 = threading.Thread(target=second)
        t2.start()
        # 0.12s is the second call's retry budget; joining past it
        # is enough to know it had its chance and was refused.
        t2.join(1.0)
        assert not second_ran.is_set(), (
            "a second thread ran while the lock was held; reentrancy "
            "is per-process, not per-thread, and the lock does nothing"
        )
    finally:
        release.set()
        t1.join(5.0)


def test_a_second_thread_is_refused_once_the_pid_file_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must not depend on winning a race with the pid file."""
    from lies.qmd.lock import with_qmd_lock

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "envelope.lock"))
    holding = threading.Event()
    release = threading.Event()
    second_ran = threading.Event()

    @with_qmd_lock(timeout_s=5.0)
    def holder() -> None:
        holding.set()
        release.wait(5.0)

    @with_qmd_lock(timeout_s=0.12)
    def second() -> None:
        second_ran.set()

    t1 = threading.Thread(target=holder)
    t1.start()
    try:
        assert holding.wait(5.0), "holder never acquired"
        # Let the holder's envelope land. Without this the contender
        # wins a race it should not depend on.
        time.sleep(0.3)
        t2 = threading.Thread(target=second)
        t2.start()
        t2.join(1.0)
    finally:
        release.set()
        t1.join(5.0)

    assert not second_ran.is_set(), (
        "a second thread entered the critical section while the lock was "
        "held; the same-pid self-recovery reaped a live lock"
    )
