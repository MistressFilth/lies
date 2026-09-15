"""Ensure and inspect qmd's own MCP daemon.

qmd owns its lifecycle: ``qmd mcp --http --daemon`` is idempotent (a
second call prints ``Already running (PID N)`` and exits 0), and
``qmd status`` reports the running pid. LIES therefore keeps no record
of qmd's pid — a second copy of that truth could only drift.

The daemon is machine-global: one fixed port, one index under
``~/.cache/qmd``. Several wikis and unrelated tools share it. That is
why this module exposes no public stop function — operators who want
to stop it run ``qmd mcp stop`` themselves. Killing it would break
sessions LIES knows nothing about, exactly like killing a host-spawned
stdio server.

The one exception is :func:`_reap_qmd_daemon`: an internal helper that
SIGTERMs the daemon when the sidecar records a different ``data-dir``
than the wiki we are about to serve. Without it, the newly-spawned
qmd would race the old daemon for the port and could inherit its
index. It is the only path where LIES reaps a daemon it does not own.

Every function here is non-fatal. A wiki server that refused to start
because its search backend was down would be a worse failure than
degraded search, so failures are reported through :class:`QmdState`
rather than raised.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path as _Path

import httpx

import fastmcp
import mcp

_log = logging.getLogger(__name__)

_QMD_BIN = "qmd"
_MCP_LINE = re.compile(r"^MCP:\s+running\s+\(PID\s+(\d+)\)", re.MULTILINE)
_ALREADY_RUNNING = re.compile(r"Already running\s+\(PID\s+(\d+)\)")

SIDECAR_PATH = _Path(
    os.environ.get("LIES_QMD_SIDECAR_OVERRIDE")
    or (_Path.home() / ".local" / "share" / "qmd" / "mcp.data-dir")
)


def read_sidecar_data_dir() -> _Path | None:
    """Read the ``data-dir`` the running qmd daemon was started with.

    Returns ``None`` when the sidecar is absent (first-run case).
    """
    if not SIDECAR_PATH.exists():
        return None
    return _Path(SIDECAR_PATH.read_text().strip())


def write_sidecar_data_dir(data_dir: _Path) -> None:
    """Record the ``data-dir`` for the current qmd daemon session.

    Called from :func:`ensure_qmd_daemon` after a successful start.
    """
    SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIDECAR_PATH.write_text(str(data_dir))


def check_data_dir_match(expected: _Path) -> bool:
    """Return True if the sidecar's data-dir matches ``expected`` (or is absent).

    Paths are normalized via :meth:`Path.resolve` before comparison so
    callers that pass ``Path("wiki")`` vs ``Path("./wiki")`` do not get
    a spurious mismatch from a leading-dot or relative-segment drift.
    """
    actual = read_sidecar_data_dir()
    if actual is None:
        return True
    return actual.resolve() == expected.resolve()


def _daemon_start_time() -> float | None:
    """mtime of qmd's mcp.pid (proxy for daemon launch time)."""
    raw = os.environ.get("XDG_CACHE_HOME", "")
    cache = _Path(raw) if raw and not raw.startswith("${") else _Path.home() / ".cache"
    pid_path = cache / "qmd" / "mcp.pid"
    try:
        return pid_path.stat().st_mtime
    except OSError:
        return None


def _global_last_write_marker() -> _Path:
    """Single machine-global sentinel, sibling of qmd's mcp.pid and mcp.data-dir.

    The qmd daemon is machine-global and indexes whatever libraries +
    wikis the operator mounts. A per-data-dir marker would miss the
    case where the daemon serves wiki A but wiki B (or any library)
    was written since the daemon started. One marker, touched by every
    write envelope (``LibraryWriter.commit`` +
    ``WikiMemoryService.apply_plan``), reflects the union of "anything
    has been written."
    """
    raw = os.environ.get("XDG_CACHE_HOME", "")
    cache = _Path(raw) if raw and not raw.startswith("${") else _Path.home() / ".cache"
    return cache / "qmd" / "last-write-marker"


def _is_daemon_stale() -> bool:
    """True when any wiki or library has been written since the daemon started."""
    marker = _global_last_write_marker()
    daemon_start = _daemon_start_time()
    if daemon_start is None or not marker.exists():
        return False
    return marker.stat().st_mtime > daemon_start


@dataclass(frozen=True)
class QmdState:
    """What LIES knows about qmd right now.

    ``detail`` is a human-readable line for ``lies mcp status`` and for
    the single stderr warning ``up`` prints when qmd is unavailable.
    """

    installed: bool
    running: bool
    pid: int | None
    detail: str


def qmd_installed() -> bool:
    """Return True if the ``qmd`` binary is on PATH."""
    return shutil.which(_QMD_BIN) is not None


def _not_installed() -> QmdState:
    return QmdState(
        installed=False,
        running=False,
        pid=None,
        detail="qmd is not installed (not on PATH); search runs degraded",
    )


def qmd_daemon_state() -> QmdState:
    """Report qmd's daemon state by parsing ``qmd status``.

    Never raises: any failure is reported as ``running=False`` with the
    reason in ``detail``.
    """
    if not qmd_installed():
        return _not_installed()
    try:
        proc = subprocess.run(
            [_QMD_BIN, "status"],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return QmdState(True, False, None, "qmd status timed out")
    except OSError as exc:
        return QmdState(True, False, None, f"qmd status failed: {exc}")

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        first = stderr.splitlines()[0] if stderr else "no stderr"
        return QmdState(True, False, None, f"qmd status exited {proc.returncode}: {first}")
    match = _MCP_LINE.search(proc.stdout or "")
    if match is None:
        return QmdState(True, False, None, "qmd daemon not running")
    pid = int(match.group(1))
    return QmdState(True, True, pid, f"qmd daemon running (pid {pid})")


def _pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is alive (and not a zombie). Never raises.

    Zombies respond to ``os.kill(pid, 0)`` with success — the PID still
    exists in the process table until the parent reaps it — but they
    have released every resource we care about (file descriptors,
    sockets, ports). Distinguishing them is what lets the reap helper
    return promptly for a daemon that exited on SIGTERM instead of
    waiting the full grace for an already-dead process.
    """
    status_path = f"/proc/{pid}/status"
    try:
        with open(status_path) as f:
            for line in f:
                if line.startswith("State:"):
                    state = line.split(":", 1)[1].strip()
                    return not state.startswith(("Z", "X"))
    except OSError:
        return False
    return False


def _reap_qmd_daemon(*, grace: float = 2.0, poll: float = 0.05) -> None:
    """Kill the running qmd daemon process and wait for it to exit.

    SIGTERM first; if the process is still alive past ``grace`` seconds,
    escalate to SIGKILL. ``poll`` is the wait-loop interval. Never
    raises. Without the wait, the subsequent :func:`_spawn_qmd_daemon`
    could race the dying daemon for the port and inherit its index,
    while the sidecar already recorded the new ``data-dir``.
    """
    state = qmd_daemon_state()
    if not state.running or state.pid is None:
        return
    pid = state.pid

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(poll)

    # Past grace — escalate.
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(poll)


def _spawn_qmd_daemon() -> None:
    """Invoke ``qmd mcp --http --daemon``. Never raises."""
    try:
        subprocess.run(
            [_QMD_BIN, "mcp", "--http", "--daemon"],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def ensure_qmd_daemon(*, data_dir: _Path, timeout: float = 15.0) -> QmdState:
    """Start qmd's http daemon if not already up; reap and respawn if the
    sidecar records a different ``data-dir`` OR the daemon is serving a
    stale index (F14: marker mtime > daemon pidfile mtime).
    """
    if not qmd_installed():
        return _not_installed()

    if not check_data_dir_match(data_dir):
        # Foreign daemon serving the wrong index; reap and respawn.
        _reap_qmd_daemon()
        _spawn_qmd_daemon()
        write_sidecar_data_dir(data_dir)
        return qmd_daemon_state()

    if _is_daemon_stale():
        # F14: the wiki has been written since the daemon started.
        # Reap+respawn via the sync path (no probe poll — ensure_qmd_daemon
        # is sync and called from sync CLI + LibraryWriter envelopes; making
        # it async would ripple into every caller).
        _log.info("ensure_qmd_daemon: daemon stale by marker; reaping via F14")
        _reap_qmd_daemon()
        _spawn_qmd_daemon()
        write_sidecar_data_dir(data_dir)
        return qmd_daemon_state()

    # Normal path: idempotent start.
    try:
        proc = subprocess.run(
            [_QMD_BIN, "mcp", "--http", "--daemon"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return QmdState(True, False, None, f"starting qmd timed out after {timeout:g}s")
    except OSError as exc:
        return QmdState(True, False, None, f"starting qmd failed: {exc}")
    write_sidecar_data_dir(data_dir)
    output = f"{proc.stdout or ''}{proc.stderr or ''}"
    already = _ALREADY_RUNNING.search(output)
    if already is not None:
        pid = int(already.group(1))
        return QmdState(True, True, pid, f"qmd daemon already running (pid {pid})")
    if proc.returncode != 0:
        first = output.strip().splitlines()[0] if output.strip() else "no output"
        return QmdState(True, False, None, f"qmd exited {proc.returncode}: {first}")
    return qmd_daemon_state()


# --- Added for the qmd-daemon-recycle spec (2026-09-14) ---


class QmdRecycleFailed(RuntimeError):
    """qmd daemon recycled but never served within ready_timeout.

    Carries the last observed ``QmdState`` so callers can branch on the
    concrete failure reason (no listener, exit code, timeout) without
    parsing the message string.
    """

    def __init__(self, ready_timeout_s: float, last_state: "QmdState") -> None:
        super().__init__(
            f"qmd daemon recycled but never served within "
            f"{ready_timeout_s:g}s (last state: {last_state.detail})"
        )
        self.ready_timeout_s = ready_timeout_s
        self.last_state = last_state


async def recycle_qmd_daemon(
    *,
    data_dir: "_Path",
    daemon_url: str,
    ready_timeout: float = 30.0,
    poll_interval: float = 0.5,
) -> "QmdState":
    """Restart qmd's daemon; poll list_tools() until it serves or budget expires.

    Always reap first (idempotent when not running), then spawn. The
    probe is a ``fastmcp.Client.list_tools()`` call against
    ``daemon_url`` — proves the JSON-RPC session is alive (not whether
    qexpander is warm; warm-up latency is borne by the first real call).

    Raises:
        QmdRecycleFailed: reap+spawn+probe never served within ready_timeout.
    """
    _log.debug("recycle_qmd_daemon: reaping")
    _reap_qmd_daemon()
    _log.debug("recycle_qmd_daemon: spawning")
    _spawn_qmd_daemon()
    _log.debug("recycle_qmd_daemon: writing sidecar for %s", data_dir)
    write_sidecar_data_dir(data_dir)

    deadline = asyncio.get_event_loop().time() + ready_timeout
    last_state = qmd_daemon_state()
    while asyncio.get_event_loop().time() < deadline:
        try:
            assert fastmcp is not None  # type: ignore[assertion-that-fails]
            async with fastmcp.Client(daemon_url) as client:  # type: ignore[attr-defined]
                await client.list_tools()
            _log.debug(
                "recycle_qmd_daemon: probe succeeded after %gs",
                ready_timeout - (deadline - asyncio.get_event_loop().time()),
            )
            return qmd_daemon_state()
        except (httpx.HTTPError, mcp.MCPError, OSError) as exc:
            _log.debug("recycle_qmd_daemon: probe failed (%s); retrying", exc)
            last_state = qmd_daemon_state()
            await asyncio.sleep(poll_interval)
    raise QmdRecycleFailed(ready_timeout, last_state)
