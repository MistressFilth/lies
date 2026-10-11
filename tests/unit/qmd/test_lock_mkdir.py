"""The qmd lock's per-acquisition setup: resolve once, ``mkdir`` once.

These count ``mkdir`` calls rather than timing them. A contended wait on
the async gate runs one attempt per poll for up to 120 s, and the cost
under test is that the attempt used to re-create a directory that
already existed, on every single attempt.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from lies.lock_errors import QmdLockBusy
from lies.qmd import lock as qmd_lock
from lies.utils import exclusive
from lies.utils.lock_heartbeat import AcquireResult

# --- how the count is taken ---------------------------------------------
#
# Patching `Path.mkdir` catches every caller, including the recursive
# `parents=True` walk, so the number is about the operation rather than
# about one particular spelling of it.


@pytest.fixture
def count_mkdir(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every ``Path.mkdir`` the test causes."""
    seen: list[Path] = []
    real = Path.mkdir

    def counting(self: Path, *a: Any, **kw: Any) -> None:
        seen.append(self)
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", counting)
    return seen


def _force_contention(monkeypatch: pytest.MonkeyPatch, attempts: list[int]) -> None:
    """Make every acquire attempt lose, without bypassing the code under test.

    Wraps the *real* ``acquire_create_lock`` so the ``parent_prepared``
    plumbing and the mkdir path both execute for real; only the outcome
    is forced. Wrapping rather than replacing matters — a stub would
    make this test pass without the hoist doing anything.
    """
    real = qmd_lock.acquire_create_lock

    def busy(path: Path, **kw: Any) -> AcquireResult:
        attempts.append(1)
        result = real(path, **kw)
        if result is not None and result.fd >= 0:
            os.close(result.fd)
        return AcquireResult(fd=-1, holder_pid=4242, status="busy")

    monkeypatch.setattr(qmd_lock, "acquire_create_lock", busy)


def test_a_contended_acquisition_mkdirs_exactly_once(
    count_mkdir: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One acquisition, many attempts, one ``mkdir``.

    Re-introducing the per-attempt ``mkdir`` at the top of
    ``acquire_create_lock`` makes this count equal the number of
    attempts. That is the mutation this test exists to catch.
    """
    # Point the lock at tmp_path. Without this the poll resolves the
    # *machine's* real `~/.local/state/lies/qmd.lock`, and the forced
    # contention below leaves a real lock file behind for every other
    # test in the run to trip over.
    lock_path = tmp_path / "state" / "lies" / "qmd.lock"
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(lock_path))
    # Pre-create the parent so the fixture itself is not in the count;
    # the mkdir under test is the one _prepare issues inside the call.
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    count_mkdir.clear()

    attempts: list[int] = []
    _force_contention(monkeypatch, attempts)
    monkeypatch.setattr(qmd_lock, "_POLL_INTERVAL_S", 0.0)

    with pytest.raises(QmdLockBusy):
        qmd_lock._acquire_with_poll(retry_budget_s=0.02, max_age_s=1800.0)

    assert len(attempts) > 3, (
        f"the poll only ran {len(attempts)} times; this test needs a "
        "genuinely contended acquisition to mean anything"
    )
    assert len(count_mkdir) == 1, (
        f"{len(count_mkdir)} mkdir calls across {len(attempts)} attempts; "
        "the parent is created once per acquisition, not once per attempt"
    )


async def test_the_async_gate_hoists_the_mkdir_too(
    count_mkdir: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async path hoists it exactly like the sync one.

    The defect was found on this path: it polls at 10 Hz for up to
    120 s, where the sync decorator polls for 30 s, so it does four
    times the redundant ``mkdir`` calls.
    """
    lock_path = tmp_path / "state" / "lies" / "qmd.lock"
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(lock_path))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    count_mkdir.clear()

    attempts: list[int] = []
    _force_contention(monkeypatch, attempts)
    monkeypatch.setattr(qmd_lock, "_POLL_INTERVAL_S", 0.0)

    with pytest.raises(QmdLockBusy):
        await qmd_lock._acquire_with_poll_async(retry_budget_s=0.02, max_age_s=1800.0)

    assert len(attempts) > 3
    assert len(count_mkdir) == 1, (
        f"{len(count_mkdir)} mkdir calls across {len(attempts)} async attempts"
    )


def test_the_first_acquire_still_creates_a_missing_parent(tmp_path: Path) -> None:
    """A caller that does not pre-prepare gets the mkdir it always got.

    The optimisation is opt-in. Without this, hoisting the mkdir would
    silently break the one caller that needs it: a first-ever acquire
    into a directory that does not exist yet.
    """
    lock_path = tmp_path / "brand" / "new" / "dir" / "qmd.lock"
    assert not lock_path.parent.exists()

    result = exclusive.acquire_create_lock(lock_path, max_age_s=1800.0)

    assert result is not None and result.status == "acquired"
    assert lock_path.parent.is_dir()
    assert lock_path.exists()

    exclusive.release_create_lock(lock_path, result.fd)


def test_parent_prepared_still_heals_a_vanished_parent(tmp_path: Path) -> None:
    """Hoisting the mkdir must not turn a vanished parent into a failure.

    Today the per-attempt mkdir heals a directory removed underneath a
    wait, silently. With the mkdir hoisted, a caller that prepared once
    would otherwise get ``FileNotFoundError`` out of the create. So the
    create heals it — on the exceptional path only.
    """
    lock_path = tmp_path / "state" / "qmd.lock"
    qmd_lock._prepare((lock_path, Path(f"{lock_path}.pid"), Path(f"{lock_path}.state.json")))
    assert lock_path.parent.is_dir()

    # Something removes the directory between prepare and create.
    lock_path.parent.rmdir()
    assert not lock_path.parent.exists()

    result = exclusive.acquire_create_lock(
        lock_path,
        max_age_s=1800.0,
        parent_prepared=True,
    )
    assert result is not None and result.status == "acquired"
    assert lock_path.exists()

    exclusive.release_create_lock(lock_path, result.fd)


def test_the_heal_covers_the_reap_and_retry_creates_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vanished-parent heal must reach *every* create, not just the first.

    This is the path an earlier version of the mkdir hoist broke. The
    heal sat around the first ``_create_exclusive`` call only, while
    three retry creates sat inside the ``FileExistsError`` handler with
    no such arm — so with the parent removed as the code entered the
    reap-and-retry branch, ``FileNotFoundError`` escaped. The first
    create healed correctly, which is exactly what made it look fine.

    Driven through the real force-repair branch, with the parent removed
    by the ``_reap_files`` the branch itself performs.
    """
    lock_path = tmp_path / "state" / "qmd.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path = Path(f"{lock_path}.pid")
    state_path = Path(f"{lock_path}.state.json")

    # A held lock with a full envelope, so the branch is entered.
    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    pid_path.write_text("999999", encoding="utf-8")  # dead pid
    state_path.write_text('{"pid": 999999, "started_at": 1e9}', encoding="utf-8")

    # Remove the parent the way a concurrent cleanup would, at the exact
    # moment the reap runs — i.e. after the contention is noticed and
    # before the retry create.
    real_reap = exclusive._reap_files

    def reap_then_vanish(create_lock: Path, pid: Path, state: Path) -> None:
        real_reap(create_lock, pid, state)
        for child in create_lock.parent.iterdir():
            child.unlink()
        create_lock.parent.rmdir()

    monkeypatch.setattr(exclusive, "_reap_files", reap_then_vanish)

    result = exclusive.acquire_create_lock(
        lock_path,
        max_age_s=1800.0,
        pid_path=pid_path,
        state_json_path=state_path,
        parent_prepared=True,
        force_repair=True,
    )

    assert result is not None and result.status == "dead_reaped", (
        "the reap-and-retry create must heal a vanished parent instead of "
        "propagating FileNotFoundError out of acquire_create_lock"
    )
    assert lock_path.exists()
    os.close(fd)
    exclusive.release_create_lock(
        lock_path, result.fd, pid_path=pid_path, state_json_path=state_path
    )


def test_ensure_lock_parent_is_a_noop_on_an_existing_directory(
    tmp_path: Path, count_mkdir: list[Path]
) -> None:
    """The helper is the mkdir, and it is idempotent.

    Pins the two things a caller relies on: it does not raise when the
    directory is already there, and it issues exactly one ``mkdir``.
    """
    target = tmp_path / "a" / "b" / "c" / "qmd.lock"
    target.parent.mkdir(parents=True, exist_ok=True)
    count_mkdir.clear()

    exclusive.ensure_lock_parent(target)

    assert len(count_mkdir) == 1
    assert target.parent.is_dir()
