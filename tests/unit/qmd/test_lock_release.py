"""A clean qmd-flock release removes the sidecars it wrote.

**The defect this closes.** ``_release`` and ``_register_holder`` each
re-resolved ``_lock_paths()`` on their own. The acquire resolved it
first, so anything that moved ``LIES_QMD_LOCK_PATH`` between the two
put the two halves of one acquisition on different files: the release
unlinked a lock nobody held and left the create-lock, the ``.pid``
sidecar and the ``.state.json`` heartbeat on disk with no fd pointing at
any of them. Measured before the fix, through the production decorator:

    A triad after : ['probeA.lock', 'probeA.lock.pid', 'probeA.lock.state.json']
    B triad after : []

Both poll loops now return the winning ``_Attempt``, which carries the
triad it ran against, and both release paths hand that to ``_release``.
The property is therefore "release removes what acquire wrote", not
"release removes whatever the environment says right now".

**The other half is the session fixture.** A test that acquires and then
never unwinds — an interrupt mid-embed, a crash in a finalizer — still
strands the pair, and because the path carries the session pid no later
session can ever reap them. ``tests/conftest.py``'s session-scoped
autouse teardown bounds that; ``test_the_session_teardown_removes_a_leaked_triad``
pins the teardown itself, since a fixture that silently stopped running
would look exactly like the behaviour it replaced.

No real sleeps and no real daemons: every case here is an in-process
acquire at ``LIES_QMD_LOCK_PATH`` under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lies.qmd import lock as qmd_lock
from tests.conftest import qmd_test_lock_path, release_qmd_test_lock


def _triad(lock_path: Path) -> tuple[Path, Path, Path]:
    """The create-lock, its heartbeat and its pid sidecar."""
    return (lock_path, Path(f"{lock_path}.state.json"), Path(f"{lock_path}.pid"))


def _clear(lock_path: Path) -> None:
    for path in _triad(lock_path):
        path.unlink(missing_ok=True)


def _assert_triad_absent(lock_path: Path, when: str) -> None:
    left = [path.name for path in _triad(lock_path) if path.exists()]
    assert not left, (
        f"{when} left {left} beside {lock_path.name}. A leaked .state.json is "
        "not inert: it is what the next acquire reads to decide whether the "
        "lock in front of it is stale."
    )


def test_a_clean_acquire_and_release_leaves_no_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline property, on the production decorator."""
    lock_path = tmp_path / "qmd.lock"
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(lock_path))

    @qmd_lock.with_qmd_lock(timeout_s=5.0)
    def _hold() -> None:
        # Mid-hold: all three exist. Asserted here because a decorator
        # that never wrote the sidecars would pass the after-check
        # vacuously.
        assert all(path.exists() for path in _triad(lock_path)), (
            "the acquire did not create the full triad, so the after-check would prove nothing"
        )

    _hold()

    _assert_triad_absent(lock_path, "a clean with_qmd_lock release")


def test_a_clean_async_acquire_and_release_leaves_no_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same property on the async twin, which the daemon query path uses."""
    lock_path = tmp_path / "qmd.lock"
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(lock_path))

    async def _hold() -> None:
        async with qmd_lock.qmd_lock_async(timeout_s=5.0):
            assert all(path.exists() for path in _triad(lock_path))

    asyncio.run(_hold())

    _assert_triad_absent(lock_path, "a clean qmd_lock_async release")


def test_release_unlinks_the_triad_it_acquired_not_the_one_the_env_now_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, driven through the decorator.

    ``LIES_QMD_LOCK_PATH`` moves while the lock is held. Pre-fix the
    release resolved the *new* path, unlinked three files that were never
    created, and left the acquired triad behind. Post-fix it unlinks the
    acquired triad and never touches the other.
    """
    acquired = tmp_path / "acquired.lock"
    moved_to = tmp_path / "moved.lock"
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(acquired))

    @qmd_lock.with_qmd_lock(timeout_s=5.0)
    def _hold() -> None:
        # The move. Nothing in production does this — it is the test
        # fixture's own teardown ordering that used to do it, and the
        # shape a host with two sessions gets if one reassigns the var.
        monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(moved_to))

    _hold()

    _assert_triad_absent(acquired, "a release whose env var moved mid-hold")
    assert not moved_to.exists(), (
        "the release unlinked a path it never acquired — it would delete "
        "another session's live lock"
    )


def test_the_holder_registration_lands_on_the_acquired_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat must describe the lock that is actually held.

    ``_register_holder`` re-resolved independently of the acquire, so the
    same move as above wrote the pid + heartbeat beside a *different*
    lock. The next acquire against the real lock then reads a heartbeat
    belonging to nothing and classifies staliness from the wrong holder.
    """
    acquired = tmp_path / "acquired.lock"
    moved_to = tmp_path / "moved.lock"
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(acquired))

    lock_path, pid_path, state_path = qmd_lock._lock_paths()
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(moved_to))

    result = qmd_lock.acquire_create_lock(
        lock_path,
        max_age_s=1800.0,
        pid_path=pid_path,
        state_json_path=state_path,
        parent_prepared=True,
    )
    assert result is not None and result.status == "acquired"
    qmd_lock._register_holder(result.fd, (lock_path, pid_path, state_path))

    assert pid_path.exists(), "the holder pid was written beside the wrong lock"
    assert state_path.exists(), "the heartbeat was written beside the wrong lock"
    assert not Path(f"{moved_to}.pid").exists()

    qmd_lock._release(result.fd, (lock_path, pid_path, state_path))


def test_the_session_teardown_removes_a_leaked_triad() -> None:
    """The conftest function that bounds the un-unwinding case.

    Simulates a test that acquired and died: all three files present, no
    fd, nobody coming back. Calls :func:`tests.conftest.release_qmd_test_lock`
    — the same body the session fixture calls — rather than a copy,
    because a copy is what drifts.
    """
    lock_path = qmd_test_lock_path()
    _clear(lock_path)
    try:
        for path in _triad(lock_path):
            path.write_text("held", encoding="utf-8")
        assert all(path.exists() for path in _triad(lock_path))

        release_qmd_test_lock()

        _assert_triad_absent(lock_path, "the session teardown")
    finally:
        _clear(lock_path)


def test_the_session_lock_path_is_the_one_the_env_var_is_pinned_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixture and the pin must not drift into two different paths.

    They were spelled independently: the env var an inline f-string in
    ``_isolated_xdg``, the cleanup a path built elsewhere. A cleanup
    pointing somewhere else is a no-op that reads as a pass. Asserted by
    behaviour — pin the env var the way ``_isolated_xdg`` does, stage the
    files ``_lock_paths`` resolves, and require the teardown to take them.
    """
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(qmd_test_lock_path()))
    resolved = qmd_lock._lock_paths()
    for path in resolved:
        path.write_text("held", encoding="utf-8")

    try:
        release_qmd_test_lock()
        for path in resolved:
            assert not path.exists(), (
                f"{path.name} survived the session teardown; it is pinned to a "
                "different path than the one _isolated_xdg points tests at"
            )
    finally:
        for path in resolved:
            path.unlink(missing_ok=True)
