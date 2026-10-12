"""A clean qmd-flock release removes the sidecars it wrote."""

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
    """The defect, driven through the decorator."""
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
    """The heartbeat must describe the lock that is actually held."""
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
    """The conftest function that bounds the un-unwinding case."""
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
    """The fixture and the pin must not drift into two different paths."""
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
