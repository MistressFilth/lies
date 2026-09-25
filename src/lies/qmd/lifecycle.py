"""qmd daemon lifecycle: status, up, down, recycle.

Mirrors ``repo/ask/scripts/qmd-daemon.py`` so LIES inherits the
same recycle-on-failure discipline ask has used in production for
years.

The qmd daemon writes its own pidfile to
``$XDG_CACHE_HOME/qmd/mcp.pid`` (see
``@tobilu/qmd/dist/cli/qmd.js``: ``cacheDir = $XDG_CACHE_HOME/qmd``,
``writeFileSync(pidPath, String(child.pid))`` in the ``--daemon``
branch). ``qmd mcp stop`` reads the same path and SIGTERMs the
recorded PID before unlinking it. This module reads + reports on
that file; it does NOT maintain its own pidfile. (An earlier draft
of the spec assumed LIES would write its own pidfile to
``$XDG_STATE_HOME/lies/qmd.lock.pid``; that path is not where qmd
writes, so following the draft verbatim would leave ``status()``
permanently reporting ``running=False`` because the pid never lands
in the LIES-side file.)

All subprocess calls in this module use DEVNULL pipes to avoid
pipe-buffer deadlocks — see :mod:`lies.qmd._subprocess`.
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

    qmd's ``mcp --http --daemon`` writes this file (see the
    ``writeFileSync(pidPath, String(child.pid))`` call in
    ``@tobilu/qmd/dist/cli/qmd.js``). ``mcp stop`` reads it. We mirror
    those locations so ``status()`` reports the same pid qmd itself
    does, and so an in-process ``_up()`` followed by ``status()`` is
    internally consistent.
    """
    from lies.xdg import cache_home

    return cache_home() / "qmd" / "mcp.pid"


def _logfile() -> Path:
    """Path to qmd's own log file (``$XDG_CACHE_HOME/qmd/mcp.log``).

    Surfaced for error messages only; qmd owns the lifecycle of this
    file (truncates on each daemon start). LIES never writes to it.
    """
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


def _find_qmd() -> str:
    path = shutil.which("qmd")
    if path is None:
        msg = "qmd binary not found on PATH"
        raise RuntimeError(msg)
    return path


def status(port: int = _DEFAULT_PORT) -> DaemonStatus:
    """Return current daemon status.

    ``running`` is true only when *both* a pidfile exists with a
    parseable integer *and* the daemon's port accepts a TCP
    connection. The pid is reported only when the port is up — a
    stale pidfile (e.g. daemon crashed) returns ``pid=None`` so
    callers do not signal a dead process.
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

    ``qmd mcp --http --daemon`` double-forks: the launched process
    writes qmd's pidfile at ``$XDG_CACHE_HOME/qmd/mcp.pid`` with
    the grandchild's PID and exits 0. We then poll ``_port_listening``
    for up to ``_READY_TIMEOUT_S`` seconds; on success we return a
    populated :class:`DaemonStatus`.

    Raises:
        RuntimeError: when the daemon does not bind the port within
            ``_READY_TIMEOUT_S`` (the operator should consult the
            qmd log at ``_logfile()`` for the failure reason).
    """
    from lies.qmd._subprocess import _run_qmd

    qmd_bin = _find_qmd()
    _run_qmd(
        [qmd_bin, "mcp", "--http", "--daemon", "--port", str(port)],
        cwd=Path(os.getcwd()),
        timeout=15.0,
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

    Short-circuits when neither a listener nor a pidfile is present
    (nothing to stop). When the pidfile is present but the port is
    closed (a crashed daemon left a stale pidfile), still calls
    ``qmd mcp stop`` — qmd itself unlinks the stale pidfile and
    exits 0, which is the desired cleanup.

    A wedged daemon that ignores ``qmd mcp stop`` past the
    ``_STOP_TIMEOUT_S`` budget is swallowed: ``recycle()``'s
    subsequent ``_up()`` will bind a fresh listener on the same port,
    superseding the stuck process. Surface-level errors raised by
    the helper are propagated (the daemon is not wedged if it
    raises).
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
        # Wedged daemon — ``qmd mcp stop`` could not unstick it
        # within budget. Fall through; recycle's subsequent _up
        # will bind a fresh listener on the same port, superseding
        # the stuck process.
        return


def serves_query(port: int = _DEFAULT_PORT, timeout: float = _SERVES_QUERY_TIMEOUT_S) -> bool:
    """True iff the daemon answers a trivial ``lex`` query within ``timeout``.

    A bound port is not enough: a daemon can pass a port probe and
    still hang on the first real query. Liveness means *serves a
    query*. This is the test that catches the qmd subprocess hung
    at 98% CPU emitting long traces — the daemon accepts TCP but
    never returns a response.

    ask's qmd-daemon.py implements the same liveness check; the
    comment there reads: "A bound port is not enough: a daemon can
    pass a port probe and still hang on the first real query."
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
