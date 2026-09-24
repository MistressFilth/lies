"""Tests for src/lies/qmd/lock.py — path constants and decorator signature."""

from __future__ import annotations

import importlib
import inspect
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest


def _reload_lock_module() -> object:
    """Re-import ``lies.qmd.lock`` so module-level constants re-resolve.

    Path constants are evaluated once at import time from
    ``$LIES_QMD_LOCK_PATH`` / ``$XDG_STATE_HOME``. The conftest's
    ``_isolated_xdg`` autouse fixture mutates ``XDG_STATE_HOME`` per
    test, and individual tests may setenv ``LIES_QMD_LOCK_PATH``. A plain
    ``import_module`` returns the cached module on subsequent calls; only
    ``reload()`` re-executes the module-level statements and re-reads the
    env vars.
    """
    return importlib.reload(importlib.import_module("lies.qmd.lock"))


def test_lock_module_imports():
    mod = importlib.import_module("lies.qmd.lock")
    assert mod is not None


def test_lock_path_default_resolves_to_xdg_state_home(monkeypatch):
    """Default path is ``${XDG_STATE_HOME:-~/.local/state}/lies/qmd.lock``.

    With neither ``LIES_QMD_LOCK_PATH`` nor ``XDG_STATE_HOME`` set, the
    resolved lock path falls back to ``~/.local/state/lies/qmd.lock``.
    """
    monkeypatch.delenv("LIES_QMD_LOCK_PATH", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    mod = _reload_lock_module()
    expected = os.path.expanduser("~/.local/state/lies/qmd.lock")
    assert str(mod._LOCK_PATH) == expected


def test_lock_path_env_override_takes_precedence(monkeypatch):
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", "/tmp/override-lies-qmd.lock")
    mod = _reload_lock_module()
    assert str(mod._LOCK_PATH) == "/tmp/override-lies-qmd.lock"


def test_pid_and_state_paths_share_lock_path_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = _reload_lock_module()
    assert mod._PID_PATH.parent == mod._LOCK_PATH.parent
    assert mod._STATE_PATH.parent == mod._LOCK_PATH.parent
    assert mod._PID_PATH.name.startswith(mod._LOCK_PATH.name)
    assert mod._STATE_PATH.name.startswith(mod._LOCK_PATH.name)


def test_with_qmd_lock_default_signature():
    """Decorator factory exposes default timeout_s=30.0 and max_age_s=1800.0."""
    from lies.qmd.lock import with_qmd_lock

    sig = inspect.signature(with_qmd_lock)
    assert "timeout_s" in sig.parameters
    assert "max_age_s" in sig.parameters
    assert sig.parameters["timeout_s"].default == 30.0
    assert sig.parameters["max_age_s"].default == 1800.0


"""Tests for the qmd flock poll-retry and release paths."""


# A small holder script that acquires the qmd flock and holds it until
# the ready marker is removed. Spawned via subprocess.Popen so the
# contending process has a different pid — required because
# ``acquire_create_lock`` self-recovers when ``stored_pid == os.getpid()``,
# which means in-process contention tests cannot reproduce cross-process
# blocking.
_HOLDER_SCRIPT = textwrap.dedent(
    """\
    import os
    import sys
    import time
    from pathlib import Path

    from lies.qmd.lock import _acquire_with_poll, _release

    ready_marker = Path(sys.argv[1])
    hold_s = float(sys.argv[2])

    fd = _acquire_with_poll(retry_budget_s=60.0, max_age_s=1800.0)
    try:
        ready_marker.write_text("ready", encoding="utf-8")
        time.sleep(hold_s)
    finally:
        _release(fd)
    """
)


def _spawn_qmd_holder(tmp_path: Path, *, hold_s: float) -> tuple[subprocess.Popen, Path]:
    """Spawn a subprocess that holds the qmd flock for ``hold_s`` seconds.

    Returns (process, ready_marker). The caller is responsible for
    terminating ``process`` if the test exits before the holder wakes
    up naturally; otherwise the lock + pid + heartbeat siblings will
    persist into the next test.
    """
    ready_marker = tmp_path / "holder_ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(ready_marker), str(hold_s)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_for_marker(ready_marker, timeout=10.0)
    return proc, ready_marker


def _wait_for_marker(marker: Path, *, timeout: float) -> None:
    deadline = time.time() + timeout
    while not marker.exists():
        if time.time() > deadline:
            raise RuntimeError(f"holder did not signal {marker} within {timeout}s")
        if not marker.parent.exists():
            raise RuntimeError(f"marker parent {marker.parent} disappeared")
        time.sleep(0.05)


def _terminate_holder(proc: subprocess.Popen, *, timeout: float = 5.0) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)


def test_with_qmd_lock_acquires_and_releases_on_clean_path(tmp_path, monkeypatch):
    """Single call against an empty lock dir: acquires, holds during with, releases on exit."""
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = importlib.reload(lock_mod)

    assert not mod._LOCK_PATH.exists()

    @mod.with_qmd_lock()
    def noop() -> str:
        return "ok"

    assert noop() == "ok"
    assert not mod._LOCK_PATH.exists()
    assert not mod._PID_PATH.exists()
    assert not mod._STATE_PATH.exists()


@pytest.mark.slow
def test_second_call_blocks_until_first_releases(monkeypatch, tmp_path):
    """Cross-process holder; main-thread acquire blocks until the holder releases."""
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = importlib.reload(lock_mod)

    # Spawn a subprocess holder; the main process is a different pid, so
    # the second acquire busy-polls until the holder releases. The hold
    # window has to be wider than the test runner's subprocess-startup
    # + acquire latency on CI runners (the original 0.05s was tight
    # enough that the holder sometimes released before the waiter
    # could observe the blocked state). The cross-process behavior
    # (busy-poll until holder releases) is what we're exercising, not
    # the wait window.
    holder, _ = _spawn_qmd_holder(tmp_path, hold_s=1.0)
    try:

        @mod.with_qmd_lock(timeout_s=5.0, max_age_s=1800.0)
        def wait_then_acquire() -> str:
            return "second"

        t = threading.Thread(target=wait_then_acquire)
        t.start()
        # Give the waiter a moment to attempt and block.
        time.sleep(0.1)
        assert t.is_alive(), "second call should be blocked while holder holds"
        # Holder releases after ~1.0s; waiter should complete shortly after.
        t.join(timeout=5)
        assert not t.is_alive(), "second call should have completed after holder released"
    finally:
        _terminate_holder(holder)


@pytest.mark.slow
def test_qmd_lock_busy_raises_after_retry_budget(monkeypatch, tmp_path):
    """First call holds forever; second call times out at timeout_s and raises QmdLockBusy."""
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]
    from lies.qmd.lock import QmdLockBusy  # type: ignore[import-not-found]
    from lies.lock_errors import WikiFlockError  # type: ignore[import-not-found]

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = importlib.reload(lock_mod)

    # Spawn a holder subprocess; its pid differs from ours so the second
    # acquire observes a live contender and busy-polls. The holder holds
    # long enough for the busy-poll to exhaust ``timeout_s``; the holder
    # is then terminated by the cleanup path so the test doesn't leak
    # processes into the next one.
    holder, _ = _spawn_qmd_holder(tmp_path, hold_s=2.0)
    try:

        @mod.with_qmd_lock(timeout_s=0.1, max_age_s=1800.0)
        def attempt() -> None:
            return None

        with pytest.raises(QmdLockBusy) as excinfo:
            attempt()
        assert excinfo.value.max_s == pytest.approx(0.1, rel=0.2)
        assert isinstance(excinfo.value, WikiFlockError)
    finally:
        _terminate_holder(holder)


@pytest.mark.slow
def test_holder_pid_in_qmd_lock_busy_when_holder_writes_heartbeat(monkeypatch, tmp_path):
    """Holder-acquire path writes pid + heartbeat. LockBusy surfaces that pid."""
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = importlib.reload(lock_mod)

    holder, _ = _spawn_qmd_holder(tmp_path, hold_s=2.0)
    try:
        # Confirm the holder's pid was registered to the pid file before
        # we attempt the contended acquire; this verifies the write path
        # is engaged by ``_acquire_with_poll`` (not just the decorator).
        _wait_for_marker(mod._PID_PATH, timeout=5.0)
        holder_pid = int(mod._PID_PATH.read_text(encoding="utf-8").strip())
        assert holder_pid != os.getpid(), "holder pid should differ from the test pid"

        @mod.with_qmd_lock(timeout_s=0.1, max_age_s=1800.0)
        def attempt() -> None:
            return None

        with pytest.raises(mod.QmdLockBusy) as excinfo:
            attempt()
        assert excinfo.value.holder_pid == holder_pid
    finally:
        _terminate_holder(holder)


def test_stale_holder_recovery_via_dead_pid(monkeypatch, tmp_path):
    """A lock file whose stored pid is dead gets reaped on the next acquire."""
    import lies.qmd.lock as lock_mod  # type: ignore[import-not-found]

    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))
    mod = importlib.reload(lock_mod)

    # Manually stage: create-lock + pid file pointing at a dead pid.
    mod._LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    mod._LOCK_PATH.touch()
    mod._PID_PATH.write_text("999999", encoding="utf-8")  # likely-dead pid

    # Acquire should reap and succeed.
    fd = mod._acquire_with_poll(retry_budget_s=1.0, max_age_s=60.0)
    try:
        assert fd >= 0
    finally:
        mod._release(fd)


"""Meta-tests: every qmd_* helper in src/lies/qmd/cli.py is wrapped."""


def test_every_qmd_helper_is_with_qmd_lock_wrapped():
    from lies.qmd import cli as qmd_cli

    qmd_callables = [
        (name, obj)
        for name, obj in vars(qmd_cli).items()
        if name.startswith("qmd_")
        and callable(obj)
        and name != "is_qmd_installed"  # doesn't shell out
    ]

    # Whitelist exception: helpers that legitimately don't shell out
    # and don't need the flock. Today only is_qmd_installed.
    expected_wrapped = {name for name, _ in qmd_callables if name != "is_qmd_installed"}

    missing: list[str] = []
    for name, obj in qmd_callables:
        if name not in expected_wrapped:
            continue
        wrapped = getattr(obj, "__wrapped__", None)
        if wrapped is None:
            missing.append(name)
    assert not missing, f"qmd helpers missing @with_qmd_lock: {missing}"


def test_is_qmd_installed_does_not_have_lock_wrapper():
    """``is_qmd_installed`` is a stub probe with no subprocess; no flock needed."""
    from lies.qmd.cli import is_qmd_installed

    assert getattr(is_qmd_installed, "__wrapped__", None) is None
