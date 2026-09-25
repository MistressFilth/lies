"""Unit tests for :mod:`lies.qmd.lifecycle`.

Only the helper functions + ``status()`` are covered here. The
spawn-a-real-daemon paths (``_up``, ``_down``) live in
``tests/integration/test_qmd_lifecycle.py`` under the
``INTEGRATION=1`` gate; spawning qmd in the unit-test loop would
race the host's actual daemon and burn 5–30 s per test, well past
the 0.15 s unit-test budget.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from lies.qmd import lifecycle
from lies.qmd.lifecycle import (
    DaemonStatus,
    _DEFAULT_PORT,
    _HOST,
    _find_qmd,
    _logfile,
    _pidfile,
    _port_listening,
    _read_pid,
    serves_query,
    status,
)


# --- DaemonStatus -----------------------------------------------------------


def test_daemon_status_repr_includes_fields() -> None:
    """The repr exposes all four fields so log lines are self-contained."""
    s = DaemonStatus(running=True, pid=4242, port=8181, url="http://127.0.0.1:8181/mcp")
    text = repr(s)
    assert "running=True" in text
    assert "pid=4242" in text
    assert "port=8181" in text
    assert "8181" in text


def test_daemon_status_stores_attributes() -> None:
    """Attributes are settable and round-trip."""
    s = DaemonStatus(running=False, pid=None, port=9000, url="http://127.0.0.1:9000/mcp")
    assert s.running is False
    assert s.pid is None
    assert s.port == 9000
    assert s.url == "http://127.0.0.1:9000/mcp"


# --- _pidfile / _logfile ----------------------------------------------------


def test_pidfile_path_under_cache_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``_pidfile()`` resolves under ``$XDG_CACHE_HOME/qmd/mcp.pid``."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # xdg.py caches via ``Path.expanduser``; the LIES override
    # (``LIES_XDG_CACHE_HOME``) takes precedence and is honored
    # here.
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path))
    assert _pidfile() == tmp_path / "qmd" / "mcp.pid"


def test_logfile_path_under_cache_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``_logfile()`` resolves under ``$XDG_CACHE_HOME/qmd/mcp.log``."""
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path))
    assert _logfile() == tmp_path / "qmd" / "mcp.log"


# --- _read_pid --------------------------------------------------------------


def test_read_pid_returns_none_when_pidfile_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing pidfile => ``None`` (not an exception)."""
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path))
    assert _read_pid() is None


def test_read_pid_parses_pidfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A well-formed pidfile yields its integer."""
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path))
    pf = tmp_path / "qmd" / "mcp.pid"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("4242\n")
    assert _read_pid() == 4242


def test_read_pid_returns_none_on_garbage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-integer pidfile is treated as missing (qmd writes garbage on crash)."""
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path))
    pf = tmp_path / "qmd" / "mcp.pid"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("not-a-pid")
    assert _read_pid() is None


# --- _port_listening --------------------------------------------------------


def test_port_listening_returns_false_for_closed_port() -> None:
    """A port nothing is bound to reports ``False``."""
    # Port 1 is privileged; bind() will fail. We want connect() to
    # fail too — that's the contract — without needing a live
    # listener. Use a high port we know is free on the loopback.
    # ``0`` is the "any" port; binding it is allowed but no one
    # listens on it, so connect() returns ConnectionRefusedError.
    # We just need a port that's definitely not in use.
    assert _port_listening(1) is False


# --- _find_qmd --------------------------------------------------------------


def test_find_qmd_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_find_qmd`` raises ``RuntimeError`` when ``qmd`` is not on PATH."""
    monkeypatch.setattr(lifecycle.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="qmd binary not found"):
        _find_qmd()


def test_find_qmd_returns_path_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_find_qmd`` returns the resolved path string from ``shutil.which``."""
    monkeypatch.setattr(lifecycle.shutil, "which", lambda _name: "/usr/local/bin/qmd")
    assert _find_qmd() == "/usr/local/bin/qmd"


# --- status -----------------------------------------------------------------


def test_status_returns_not_running_when_pidfile_absent_and_no_listener(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Empty cache dir + no listener => ``running=False``, ``pid=None``.

    Mirror of the brief's first test, but with isolation against
    the real daemon: ``LIES_XDG_CACHE_HOME`` redirects the
    pidfile path; the brief's ``XDG_STATE_HOME`` override has no
    effect on the real qmd pidfile location.
    """
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path))
    s = status()
    assert s.running is False
    assert s.pid is None


def test_status_reports_pid_when_port_listening(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pidfile + listener => ``running=True``, ``pid=<int>``.

    Mocks ``_port_listening`` so we don't need a real daemon. The
    pidfile path is isolated via ``LIES_XDG_CACHE_HOME``.
    """
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path))
    pf = tmp_path / "qmd" / "mcp.pid"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("1234\n")
    monkeypatch.setattr(lifecycle, "_port_listening", lambda _port: True)
    s = status()
    assert s.running is True
    assert s.pid == 1234


def test_status_reports_no_pid_when_pidfile_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Stale pidfile (no listener) => ``running=False``, ``pid=None``.

    Guards the contract that ``status().pid`` does not surface a
    dead PID; callers must not ``kill()`` it. Uses an ephemeral
    high port (1 is privileged and bind() refuses non-root; ``0``
    is the kernel "any" port that nothing listens on) instead of
    the default 8181 — the developer's machine typically has a
    foreground qmd bound there, which would race the test.
    """
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path))
    pf = tmp_path / "qmd" / "mcp.pid"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("9999\n")
    s = status(port=1)
    assert s.running is False
    assert s.pid is None


def test_status_reports_running_false_when_listener_but_no_pidfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A listener without a pidfile is not "our" daemon.

    Another tool may be bound to 8181; we can't manage it. Status
    reports running=False so we don't try to ``_up`` over it.
    """
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path))
    # No pidfile (tmp_path/qmd/mcp.pid absent).
    monkeypatch.setattr(lifecycle, "_port_listening", lambda _port: True)
    s = status()
    assert s.running is False
    assert s.pid is None


def test_status_url_uses_default_port() -> None:
    """The default ``status()`` URL points at the default port."""
    s = status()
    assert s.port == _DEFAULT_PORT
    assert s.url == f"http://{_HOST}:{_DEFAULT_PORT}/mcp"


def test_status_honors_custom_port() -> None:
    """``status(port=...)`` reflects the requested port in ``url``/``port``."""
    s = status(port=9000)
    assert s.port == 9000
    assert s.url == "http://127.0.0.1:9000/mcp"


# --- serves_query -----------------------------------------------------------


def test_serves_query_returns_false_when_httpx_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``serves_query`` returns False when ``httpx.Client.post`` raises.

    A bound port is not enough: ``serves_query`` is the liveness
    probe that catches a wedged daemon (TCP accepts but the query
    hangs). The exception arm here covers both ``httpx.HTTPError``
    and ``ValueError`` (malformed JSON) — we exercise the
    ``HTTPError`` arm via a mocked ``ConnectError`` without
    standing up a real daemon, keeping the unit-test budget
    well under 0.15 s.

    The function imports ``httpx`` lazily, so monkeypatching the
    module-level ``httpx.Client`` (the same object in
    ``sys.modules``) intercepts the lookup.
    """

    class _RaisingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _RaisingClient:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def post(self, *args: object, **kwargs: object) -> None:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", _RaisingClient)
    assert serves_query(timeout=1.0) is False
