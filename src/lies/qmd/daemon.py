"""Ensure and inspect qmd's own MCP daemon.

qmd owns its lifecycle: ``qmd mcp --http --daemon`` is idempotent (a
second call prints ``Already running (PID N)`` and exits 0), and
``qmd status`` reports the running pid. LIES therefore keeps no record
of qmd's pid — a second copy of that truth could only drift.

The daemon is machine-global: one fixed port, one index under
``~/.cache/qmd``. Several wikis and unrelated tools share it. That is why
this module has no stop function. Killing it would break sessions LIES
knows nothing about, exactly like killing a host-spawned stdio server.

Every function here is non-fatal. A wiki server that refused to start
because its search backend was down would be a worse failure than
degraded search, so failures are reported through :class:`QmdState`
rather than raised.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path as _Path

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
    """Return True if the sidecar's data-dir matches ``expected`` (or is absent)."""
    actual = read_sidecar_data_dir()
    return actual is None or actual == expected


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


def _reap_qmd_daemon() -> None:
    """Kill the running qmd daemon process. Never raises."""
    state = qmd_daemon_state()
    if not state.running or state.pid is None:
        return
    import signal

    try:
        os.kill(state.pid, signal.SIGTERM)
    except OSError:
        pass


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
    sidecar records a different ``data-dir``.
    """
    if not qmd_installed():
        return _not_installed()
    if not check_data_dir_match(data_dir):
        # Foreign daemon serving the wrong index; reap and respawn.
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
