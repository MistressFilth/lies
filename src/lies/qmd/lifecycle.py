"""qmd daemon lifecycle: status, up, down, recycle.

Mirrors ask's ``scripts/qmd-daemon.py``. The qmd daemon writes its
own pidfile to ``$XDG_CACHE_HOME/qmd/mcp.pid`` (``writeFileSync``
in the ``--daemon`` branch of ``@tobilu/qmd/dist/cli/qmd.js``);
``qmd mcp stop`` reads the same path. This module reads + reports
on that file; it does NOT maintain its own pidfile.

All subprocess calls use DEVNULL pipes to avoid pipe-buffer
deadlocks — see :mod:`lies.qmd._subprocess`.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

_HOST = "127.0.0.1"
_DEFAULT_PORT = 8181
_READY_TIMEOUT_S = 30.0
_STOP_TIMEOUT_S = 10.0
_SERVES_QUERY_TIMEOUT_S = 5.0
_DEFAULT_URL = f"http://{_HOST}:{_DEFAULT_PORT}"


class DaemonStatus:
    """Snapshot of qmd daemon state."""

    def __init__(self, *, running: bool, pid: int | None, port: int, url: str) -> None:
        self.running = running
        self.pid = pid
        self.port = port
        self.url = url

    def __repr__(self) -> str:
        return (
            f"DaemonStatus(running={self.running}, pid={self.pid}, "
            f"port={self.port}, url={self.url!r})"
        )


def _pidfile() -> Path:
    """Path to qmd's pidfile (``$XDG_CACHE_HOME/qmd/mcp.pid``).

    qmd's ``mcp --http --daemon`` writes this file; ``mcp stop``
    reads it.
    """
    from lies.xdg import cache_home

    return cache_home() / "qmd" / "mcp.pid"


def _logfile() -> Path:
    """Path to qmd's own log file. Surfaced for error messages only."""
    from lies.xdg import cache_home

    return cache_home() / "qmd" / "mcp.log"


def _read_pid() -> int | None:
    pf = _pidfile()
    if not pf.exists():
        return None
    try:
        return int(pf.read_text().strip())
    except (OSError, ValueError):
        return None


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((_HOST, port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


# Liveness deadlines, deliberately separate from the retrieval
# budget in ``lies.config.get_qmd_query_timeout``. A slow answer
# to "is this alive?" is itself the failure: these let ``lies
# qmd up`` and ``lies qmd status`` report a wedged daemon instead
# of blocking on it.
DAEMON_START_TIMEOUT_S = 15.0
PROBE_TIMEOUT_S = 5.0


def _find_qmd() -> str:
    path = shutil.which("qmd")
    if path is None:
        msg = "qmd binary not found on PATH"
        raise RuntimeError(msg)
    return path


def status(port: int = _DEFAULT_PORT) -> DaemonStatus:
    """Return current daemon status.

    ``running`` is true only when both a parseable pidfile exists
    *and* the daemon's port accepts a TCP connection. The pid is
    reported only when the port is up — a stale pidfile returns
    ``pid=None`` so callers do not signal a dead process.
    """
    pid = _read_pid()
    listening = _port_listening(port)
    return DaemonStatus(
        running=listening and pid is not None,
        pid=pid if listening else None,
        port=port,
        url=f"http://{_HOST}:{port}/mcp",
    )


def _up(port: int = _DEFAULT_PORT) -> DaemonStatus:
    """Spawn the daemon and wait for the port to bind.

    ``qmd mcp --http --daemon`` double-forks and writes qmd's
    pidfile with the grandchild's PID. We then poll
    ``_port_listening`` for up to ``_READY_TIMEOUT_S`` seconds.

    Raises:
        RuntimeError: when the daemon does not bind the port
            within ``_READY_TIMEOUT_S`` (operator should consult
            ``_logfile()``).
    """
    from lies.qmd._subprocess import _run_qmd

    qmd_bin = _find_qmd()
    _run_qmd(
        [qmd_bin, "mcp", "--http", "--daemon", "--port", str(port)],
        cwd=Path(os.getcwd()),
        timeout=DAEMON_START_TIMEOUT_S,
    )

    deadline = time.time() + _READY_TIMEOUT_S
    while time.time() < deadline:
        if _port_listening(port):
            return status(port)
        time.sleep(0.1)

    msg = (
        f"qmd daemon failed to bind {_HOST}:{port} within {_READY_TIMEOUT_S}s — check {_logfile()}"
    )
    raise RuntimeError(msg)


def _down(port: int = _DEFAULT_PORT) -> None:
    """Stop the daemon via ``qmd mcp stop``. Best-effort; bounded.

    Short-circuits when nothing is to stop (no listener, no
    pidfile). A wedged daemon that ignores ``qmd mcp stop`` past
    ``_STOP_TIMEOUT_S`` is swallowed: ``recycle()``'s subsequent
    ``_up()`` will bind a fresh listener on the same port,
    superseding the stuck process. Surface-level errors propagate
    (the daemon is not wedged if it raises).
    """
    from lies.qmd._subprocess import _run_qmd

    if not _port_listening(port) and _read_pid() is None:
        return

    qmd_bin = _find_qmd()
    try:
        _run_qmd(
            [qmd_bin, "mcp", "stop"],
            cwd=Path(os.getcwd()),
            timeout=_STOP_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, RuntimeError):
        return


def serves_query(port: int = _DEFAULT_PORT, timeout: float = _SERVES_QUERY_TIMEOUT_S) -> bool:
    """True iff the daemon answers a trivial ``lex`` query within ``timeout``.

    A bound port is not enough — a daemon can pass a port probe
    and still hang on the first real query. This catches the qmd
    subprocess hung at 98% CPU emitting long traces (TCP accepted,
    no response).
    """
    import httpx

    payload = {"searches": [{"type": "lex", "query": "ready"}], "limit": 1}
    try:
        with httpx.Client(base_url=f"http://{_HOST}:{port}", timeout=timeout) as client:
            resp = client.post(f"http://{_HOST}:{port}/query", json=payload)
            resp.raise_for_status()
            resp.json()
    except (httpx.HTTPError, ValueError):
        return False
    return True


def recycle(port: int = _DEFAULT_PORT, ready_timeout: float = _READY_TIMEOUT_S) -> DaemonStatus:
    """Restart the daemon and wait until it serves a query.

    Stop the listener, start a fresh one, poll ``serves_query``
    until it answers or ``ready_timeout`` elapses. Raises
    ``RuntimeError`` when the fresh daemon never serves within
    the budget.
    """
    _down(port)
    status_ = _up(port)
    deadline = time.time() + ready_timeout
    while time.time() < deadline:
        if serves_query(port, timeout=PROBE_TIMEOUT_S):
            return status_
        time.sleep(0.5)
    raise RuntimeError(f"qmd daemon recycled but never served within {ready_timeout}s")
