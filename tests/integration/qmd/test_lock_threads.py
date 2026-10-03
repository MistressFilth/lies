"""The qmd lock is reentrant on one thread and still exclusive across threads.

Split out of ``tests/unit/qmd/test_lock.py`` because proving a second
thread is *refused* means waiting out its retry budget, and thread
scheduling does not fit reliably inside the 0.15s unit budget. The
same-thread half lives in the unit file and runs on every unit run;
this file is only the cross-thread half. No ``skipif`` — the
integration gate is the same one every other integration test honours.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest


def test_a_second_thread_still_contends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second thread must still serialize behind the first.

    If reentrancy were tracked in a plain module global, a
    concurrent thread would see a non-zero depth and skip the
    acquire entirely — turning the lock off.
    """
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
