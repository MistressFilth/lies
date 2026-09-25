"""Integration tests for :mod:`lies.qmd.lifecycle` ``_up`` / ``_down``.

Every test here spawns a real ``qmd mcp --http --daemon`` process
or stops one. Auto-skipped unless ``INTEGRATION=1`` is set in the
environment (see ``tests/integration/conftest.py``).

These tests are NOT idempotent w.r.t. the host's actual qmd
daemon: ``_up`` will refuse to start if ``$XDG_CACHE_HOME/qmd/mcp.pid``
already references a live PID, and ``_down`` will SIGTERM whatever
is bound to port 8181. To keep them runnable on a host that
already runs qmd in the foreground (the typical developer setup),
every test starts by snapshotting ``status()`` and skips when a
daemon is already up. Tests redirect ``LIES_XDG_CACHE_HOME`` into
``tmp_path`` so the pidfile they manipulate is the throwaway one,
not the host's ``~/.cache/qmd/mcp.pid``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from lies.qmd import lifecycle


def _redirected_cache_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``cache_home()`` at ``tmp_path`` so qmd writes its pidfile there.

    ``LIES_XDG_CACHE_HOME`` overrides the spec ``XDG_CACHE_HOME``
    per :func:`lies.xdg.cache_home`. ``monkeypatch.setenv`` is enough
    because ``cache_home`` resolves on every call (no module-level
    cache); the qmd child process inherits the same env via
    ``subprocess.run``'s default ``env=...`` passthrough.
    """
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path))
    return tmp_path


def _skip_if_daemon_already_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Skip when the host's real daemon is bound to 8181.

    Even with a redirected ``LIES_XDG_CACHE_HOME``, ``qmd mcp --http
    --daemon`` reads ``$XDG_CACHE_HOME/qmd/mcp.pid`` — which now
    points inside ``tmp_path`` — and starts a fresh listener on
    8181. If port 8181 is already held by the developer's
    foreground daemon, the child will fail to bind and the parent
    will report "EADDRINUSE". Skip before any test exercises the
    spawn path so the host's daemon is never accidentally clobbered.
    """
    if lifecycle._port_listening(8181):
        pytest.skip("host already has a daemon on 8181; lifecycle tests would conflict")


def test_up_starts_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``_up`` spawns a real daemon; ``status()`` reports it.

    Pins the end-to-end contract: spawn → bind → pidfile write →
    status reads the same pid qmd itself recorded. Cleans up via
    ``_down`` in the finally block so a test failure does not leave
    a daemon running.
    """
    _redirected_cache_home(monkeypatch, tmp_path)
    _skip_if_daemon_already_running(monkeypatch, tmp_path)

    s = lifecycle._up()
    try:
        assert s.running is True, f"up returned {s!r}; expected running=True"
        assert s.pid is not None
    finally:
        lifecycle._down()
        # _down's ``qmd mcp stop`` returns promptly but the kernel
        # needs a beat to release the port. Without the sleep, the
        # next test would race the lingering TIME_WAIT socket and
        # see the port as held.
        time.sleep(0.5)


def test_down_stops_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``_down`` stops a daemon; ``status()`` reports not-running."""
    _redirected_cache_home(monkeypatch, tmp_path)
    _skip_if_daemon_already_running(monkeypatch, tmp_path)

    lifecycle._up()
    lifecycle._down()
    # Give the daemon a moment to release the port; qmd's
    # ``mcp stop`` SIGTERMs and unlinks the pidfile, but the
    # listener socket takes a beat to fully close.
    time.sleep(0.5)
    s = lifecycle.status()
    assert s.running is False, f"expected not-running after _down; got {s!r}"
    assert s.pid is None


def test_serves_query_returns_true_for_healthy_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``serves_query`` probes a real ``lex`` query and returns True.

    Pins the contract that ``serves_query`` issues an actual query
    against the daemon's REST API (not just a TCP connect), so a
    wedged-but-listening daemon is correctly flagged live. The
    daemon is spawned + torn down via the real ``_up``/``_down``
    primitives, not a mock — this is the integration surface Task 3
    (``recycle``) builds on.
    """
    _redirected_cache_home(monkeypatch, tmp_path)
    _skip_if_daemon_already_running(monkeypatch, tmp_path)

    lifecycle._up()
    try:
        assert lifecycle.serves_query(timeout=10.0) is True
    finally:
        lifecycle._down()
        # Mirror ``test_up_starts_daemon``'s post-stop sleep so the
        # next test does not race the lingering TIME_WAIT socket.
        time.sleep(0.5)


def test_serves_query_returns_false_when_port_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``serves_query`` returns False without a daemon.

    No ``_up`` and no listener: ``httpx.Client.post`` raises
    ``ConnectError`` on connect, the ``HTTPError`` arm catches it,
    and ``serves_query`` returns False. The skip-if-running guard
    is required because the developer's foreground daemon would
    otherwise happily answer and the test would silently report
    True when it should report False.
    """
    _redirected_cache_home(monkeypatch, tmp_path)
    _skip_if_daemon_already_running(monkeypatch, tmp_path)

    assert lifecycle.serves_query(timeout=2.0) is False


def test_recycle_brings_wedged_daemon_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """recycle() recovers a wedged daemon the live ``_up`` cannot reach.

    Starts a real daemon via ``_up``, SIGKILLs the pidfile-recorded
    process out-of-band (bypassing ``qmd mcp stop`` -- that is
    precisely the wedge scenario ``recycle`` exists to recover
    from), waits for the kernel to release the port, then calls
    ``recycle`` and verifies the new daemon is up. Cleans up via
    ``_down`` in a ``finally`` so a failing assertion does not
    leave a daemon running.
    """
    import os

    _redirected_cache_home(monkeypatch, tmp_path)
    _skip_if_daemon_already_running(monkeypatch, tmp_path)

    lifecycle._up()
    # Simulate the wedge by killing the listener directly, bypassing
    # ``qmd mcp stop`` -- that command is exactly what _down tries
    # first; out-of-band killing is what a wedged process looks like.
    pid = lifecycle._read_pid()
    if pid is not None:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    # Wait for the kernel to release the port + any lingering TIME_WAIT
    # to settle before recycle's ``_up`` tries to bind again.
    time.sleep(1.0)

    try:
        s = lifecycle.recycle()
        assert s.running is True, f"recycle returned {s!r}; expected running=True"
    finally:
        lifecycle._down()
        # Mirror ``test_up_starts_daemon``'s post-stop sleep so the
        # next test does not race the lingering TIME_WAIT socket.
        time.sleep(0.5)


def test_recycle_raises_when_daemon_never_serves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """recycle() raises ``RuntimeError`` when no daemon can serve within budget.

    Holds port 8181 with a raw socket so the fresh ``_up`` cannot
    bind a working daemon and ``recycle``'s poll loop cannot get a
    sane liveness signal. ``ready_timeout=2`` keeps the test bounded;
    the blocker is released in ``finally`` so the next test does not
    see a port conflict. The skip-if-running guard runs BEFORE the
    bind: we want to skip if the host's real daemon is already on
    8181, not skip after we've claimed the port for our blocker.
    """
    import socket

    _redirected_cache_home(monkeypatch, tmp_path)
    _skip_if_daemon_already_running(monkeypatch, tmp_path)

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        blocker.bind(("127.0.0.1", 8181))
    except OSError:
        # Lost a race against the host's real daemon coming up
        # between the skip check and the bind. Skip rather than fail.
        blocker.close()
        pytest.skip("port grabbed after _skip_if_daemon_already_running check")
    blocker.listen(1)
    try:
        with pytest.raises(RuntimeError):
            lifecycle.recycle(ready_timeout=2.0)
    finally:
        blocker.close()
        # Let the kernel release the port + the lingering TIME_WAIT
        # settle before the next test runs.
        time.sleep(0.5)
