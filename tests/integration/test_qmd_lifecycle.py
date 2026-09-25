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
