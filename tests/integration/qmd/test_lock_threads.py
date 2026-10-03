"""The qmd lock is reentrant on one thread and still exclusive across threads.

Split out of ``tests/unit/qmd/test_lock.py`` because it does not fit the
0.15s unit budget: proving a second thread is *refused* means waiting out
that thread's retry budget, and thread scheduling does not fit reliably
inside 0.15s (measured 0.214s). The rubric's MOVE step applies: separate
thread, timing-dependent, no qmd involved.

The property is load-bearing. ``with_qmd_lock`` gained reentrancy because
two decorated helpers compose — ``qmd_collection_add_or_update`` calls
``qmd_collection_show`` and ``qmd_collection_add``, ``qmd_cleanup`` calls
``qmd_reindex`` — and an unreentrant lock deadlocks against itself. Reentrancy
that leaked across threads would disable the lock entirely, which is what
this guards.

**What the move cost, stated so it is not mistaken for free.** The
same-thread half (``test_the_lock_is_reentrant_on_one_thread``) *does*
live in ``tests/unit/qmd/test_lock.py`` and runs on every unit run — this
file is only the cross-thread half. So a unit-only run still catches a
lock that deadlocks against itself, and only the "reentrancy leaked across
threads and turned the lock off" regression needs the slower path. That is
the division, and it is why this file is a supplement rather than the
only coverage.

**It does not skip silently.** There is no ``skipif`` here: the test runs
whenever the file is collected, and the ``tests/integration`` gate is the
same one every other integration test honours (``INTEGRATION=1``). Nothing
in this file turns a green suite into one that has quietly stopped testing
anything — the failure mode this branch produced repeatedly was a
condition that quietly excluded the check, not a check that ran in the
wrong directory.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest


def test_a_second_thread_still_contends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reentrancy is per-thread, not per-process.

    The guard above must not weaken the lock's actual purpose: a second
    thread still serializes behind the first. If reentrancy were tracked
    in a plain module global, a concurrent thread would see a non-zero
    depth and skip the acquire entirely -- turning the lock off.
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
        # 0.12s is the second call's own retry budget, so joining with a
        # margin past it is enough to know it had its chance and was
        # refused. Deliberately short: the whole test must stay inside the
        # 0.15s unit budget, and a longer poll here would blow it while
        # testing nothing extra.
        t2.join(1.0)
        # The second thread must NOT have run while the first held it.
        assert not second_ran.is_set(), (
            "a second thread ran while the lock was held; reentrancy is "
            "per-process, not per-thread, and the lock does nothing"
        )
    finally:
        release.set()
        t1.join(5.0)
