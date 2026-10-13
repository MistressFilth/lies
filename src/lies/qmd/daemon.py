"""Ensure and inspect qmd's own MCP daemon.

qmd owns its lifecycle: ``qmd mcp --http --daemon`` is idempotent and
``qmd status`` reports the running pid, so LIES keeps no record of it.
The daemon is machine-global — one fixed port, one index — which is why
this module exposes no public stop function. The one exception is
:func:`_reap_qmd_daemon`, which SIGTERMs the daemon when the sidecar
records a different scope than the one about to be served.

Every function here is non-fatal: failures are reported through
:class:`QmdState` rather than raised.
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

from lies.qmd._subprocess import _run_qmd

_log = logging.getLogger(__name__)

_QMD_BIN = "qmd"
_MCP_LINE = re.compile(r"^MCP:\s+running\s+\(PID\s+(\d+)\)", re.MULTILINE)
_ALREADY_RUNNING = re.compile(r"Already running\s+\(PID\s+(\d+)\)")

SIDECAR_PATH = _Path(
    os.environ.get("LIES_QMD_SIDECAR_OVERRIDE")
    or (_Path.home() / ".local" / "share" / "qmd" / "mcp.data-dir")
)


class SidecarOverrideNotAFile(ValueError):
    """``LIES_QMD_SIDECAR_OVERRIDE`` names a directory rather than a file.

    The variable names the one *sidecar file* qmd's daemon records its
    data-dir in. Pointing it at the directory holding that file — the
    obvious slip, since the default's parent is ``~/.local/share/qmd`` —
    makes both sidecar accessors raise ``IsADirectoryError`` from inside
    ``Path.read_text``/``write_text``, which names file I/O rather than
    the variable the operator set.

    Args:
        path: the bound sidecar path, which exists as a directory.
    """

    def __init__(self, path: _Path) -> None:
        super().__init__(
            f"LIES_QMD_SIDECAR_OVERRIDE={str(path)!r} is a directory, but it must be "
            f"a file path: it names the sidecar file the qmd daemon records its "
            f"data-dir in, not the directory holding it. Point it at a file, e.g. "
            f"{str(path / 'mcp.data-dir')!r}, or unset it to use the default "
            f"~/.local/share/qmd/mcp.data-dir."
        )
        self.path = path


def _validated_sidecar_path() -> _Path:
    """Return :data:`SIDECAR_PATH`, refusing a directory.

    Validated at first use rather than at import, even though the path is
    bound at import. An import-time raise makes this module unimportable,
    and ``tests/conftest.py`` rebinds ``SIDECAR_PATH`` *after* importing
    it — so a host carrying a bad value could not run the suite at all,
    not even to assert the error. First use still lands early: the first
    caller is :func:`check_scope_match` via :func:`ensure_qmd_daemon`, on
    the first command that touches qmd.

    Reads the module global rather than the environment, so a test that
    rebinds ``SIDECAR_PATH`` is validated against the value it bound.

    Returns:
        The bound sidecar path.

    Raises:
        SidecarOverrideNotAFile: the path exists as a directory.
    """
    if SIDECAR_PATH.is_dir():
        raise SidecarOverrideNotAFile(SIDECAR_PATH)
    return SIDECAR_PATH


def read_sidecar_scope() -> _Path | None:
    """Read the LIES *scope* recorded for the current qmd daemon session.

    Returns ``None`` when the sidecar is absent (first-run case).

    The name says scope, not data-dir, because that is what it holds.
    See :func:`_canonical_scope` for why the value has no meaning to qmd.

    Raises:
        SidecarOverrideNotAFile: :data:`SIDECAR_PATH` is a directory.
    """
    path = _validated_sidecar_path()
    if not path.exists():
        return None
    return _Path(path.read_text().strip())


def write_sidecar_scope(scope: _Path) -> None:
    """Record the LIES scope for the current qmd daemon session.

    Called from :func:`ensure_qmd_daemon` after a successful start.

    Raises:
        SidecarOverrideNotAFile: :data:`SIDECAR_PATH` is a directory.
    """
    path = _validated_sidecar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(scope))


def check_scope_match(expected: _Path) -> bool:
    """Return True if the sidecar's scope matches ``expected`` (or is absent).

    Paths are normalized via :meth:`Path.resolve` before comparison so
    callers that pass ``Path("wiki")`` vs ``Path("./wiki")`` do not get
    a spurious mismatch from a leading-dot or relative-segment drift.
    """
    actual = read_sidecar_scope()
    if actual is None:
        return True
    return actual.resolve() == expected.resolve()


def _canonical_scope() -> _Path:
    """The one scope value every caller passes.

    It answers only *"is the serving daemon the one LIES started?"* — it
    never selects an index, never reaches a spawn, and is never opened.
    One daemon per host means one scope per host. See AGENTS.md.
    """
    from lies.library.registry import library_git_root

    return library_git_root()


def daemon_port_from_url(url: str) -> int | None:
    """The explicit port in ``url``, or ``None`` when it carries none.

    ``http://localhost:8181/mcp`` -> ``8181``. A bare origin, or a
    scheme-less string with no ``:``, -> ``None``, which callers read as
    "let qmd choose its own default" rather than as port 0.

    Every reap-then-spawn must thread this through: a daemon restarted
    onto the wrong port makes the readiness probe poll the configured
    port for its whole budget while the operator's daemon is gone.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url if "//" in url else f"//{url}")
    try:
        return parts.port
    except ValueError:  # a non-numeric port is not a port
        return None


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

    Touched by every write envelope (``LibraryWriter.commit`` and
    ``WikiMemoryService.apply_plan``), so it reflects the union of
    "anything has been written".
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


# See ``lies.qmd.lifecycle`` for why this is not the retrieval budget:
# a slow answer to "is this alive?" is itself the failure, so a wedged
# daemon is reported rather than waited on.
STATUS_TIMEOUT_S = 15.0


def qmd_daemon_state() -> QmdState:
    """Report qmd's daemon state by parsing ``qmd status``.

    Never raises: any failure is reported as ``running=False`` with the
    reason in ``detail``.
    """
    if not qmd_installed():
        return _not_installed()
    try:
        proc = _run_qmd(
            [_QMD_BIN, "status"],
            cwd=_Path.cwd(),
            timeout=STATUS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return QmdState(True, False, None, "qmd status timed out")
    except OSError as exc:
        return QmdState(True, False, None, f"qmd status failed: {exc}")

    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        stderr = (stderr or "").strip()
        first = stderr.splitlines()[0] if stderr else "no stderr"
        return QmdState(True, False, None, f"qmd status exited {proc.returncode}: {first}")
    match = _MCP_LINE.search(stdout or "")
    if match is None:
        return QmdState(True, False, None, "qmd daemon not running")
    pid = int(match.group(1))
    return QmdState(True, True, pid, f"qmd daemon running (pid {pid})")


def _pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is alive (and not a zombie). Never raises.

    Zombies answer ``os.kill(pid, 0)`` with success but have released
    every resource we care about, so the reap returns promptly instead
    of waiting out its grace.
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
    """Kills the running qmd daemon and waits for it to exit.

    SIGTERM first, escalating to SIGKILL past ``grace`` seconds; ``poll``
    is the wait-loop interval. Never raises. Without the wait, the
    subsequent :func:`_spawn_qmd_daemon` could race the dying daemon for
    the port and inherit its index.
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


def _spawn_qmd_daemon(
    *,
    port: int | None = None,
    data_dir: "_Path | None" = None,
    timeout: float | None = None,
    raise_on_error: bool = False,
) -> "subprocess.CompletedProcess[bytes] | None":
    """Invokes ``qmd mcp --http --daemon``. The only place qmd is spawned.

    Args:
        port: optional ``--port``; ``None`` lets qmd apply its default.
        data_dir: optional qmd cache root, delivered as
            ``XDG_CACHE_HOME``. qmd has no ``--data-dir`` flag, and it
            appends its own ``qmd/`` segment, so ``X`` becomes ``X/qmd/``.
            The only argument here that selects an index; production
            passes neither this nor ``port``.
        timeout: seconds before SIGKILL. Defaults to
            ``STATUS_TIMEOUT_S``.
        raise_on_error: propagate a spawn failure instead of swallowing it.

    Returns:
        The ``CompletedProcess`` on success, or ``None`` when the spawn
        failed and ``raise_on_error`` is false.
    """
    argv = [_QMD_BIN, "mcp", "--http", "--daemon"]
    env_overrides: dict[str, str] = {}
    if port is not None:
        argv += ["--port", str(port)]
    if data_dir is not None:
        env_overrides["XDG_CACHE_HOME"] = str(data_dir)
    try:
        return _run_qmd(
            argv,
            cwd=_Path.cwd(),
            timeout=STATUS_TIMEOUT_S if timeout is None else timeout,
            env_overrides=env_overrides,
        )
    except subprocess.TimeoutExpired, OSError:
        if raise_on_error:
            raise
        return None


def ensure_qmd_daemon(
    *,
    scope: _Path,
    port: int | None = None,
) -> QmdState:
    """Start qmd's http daemon if not up; reap and respawn on a scope mismatch
    or a stale index (marker mtime > daemon pidfile mtime).

    Args:
        scope: the LIES scope to record in the sidecar. It does not
            select the daemon's index — see :func:`_canonical_scope`.
        port: threaded to :func:`_spawn_qmd_daemon`; ``None`` lets qmd
            default.
    """
    if not qmd_installed():
        return _not_installed()

    if not check_scope_match(scope):
        # Foreign daemon serving the wrong index; reap and respawn.
        _reap_qmd_daemon()
        _spawn_qmd_daemon(port=port)
        write_sidecar_scope(scope)
        return qmd_daemon_state()

    if _is_daemon_stale():
        # F14: the wiki has been written since the daemon started.
        # Reap+respawn via the sync path (no probe poll — ensure_qmd_daemon
        # is sync and called from sync CLI + LibraryWriter envelopes; making
        # it async would ripple into every caller).
        _log.info("ensure_qmd_daemon: daemon stale by marker; reaping via F14")
        _reap_qmd_daemon()
        _spawn_qmd_daemon(port=port)
        write_sidecar_scope(scope)
        return qmd_daemon_state()

    # Normal path: idempotent start. ``raise_on_error`` because the two
    # QmdStates below name the concrete reason to the operator, and a
    # swallowed spawn would collapse both into "starting qmd failed".
    try:
        proc = _spawn_qmd_daemon(port=port, raise_on_error=True)
    except subprocess.TimeoutExpired:
        return QmdState(True, False, None, f"starting qmd timed out after {STATUS_TIMEOUT_S:g}s")
    except OSError as exc:
        return QmdState(True, False, None, f"starting qmd failed: {exc}")
    assert proc is not None  # raise_on_error=True means a return implies success
    write_sidecar_scope(scope)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    output = f"{stdout or ''}{stderr or ''}"
    already = _ALREADY_RUNNING.search(output)
    if already is not None:
        pid = int(already.group(1))
        return QmdState(True, True, pid, f"qmd daemon already running (pid {pid})")
    if proc.returncode != 0:
        first = output.strip().splitlines()[0] if output.strip() else "no output"
        return QmdState(True, False, None, f"qmd exited {proc.returncode}: {first}")
    return qmd_daemon_state()


# --- the qmd-daemon-recycle surface --------------------------------------


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


#: Exceptions the readiness probe treats as "not serving yet".
#:
#: ``RuntimeError`` is required: fastmcp wraps every connect failure in a
#: bare ``RuntimeError`` whose cause is a vendored ``httpx2`` error, which
#: matches neither ``httpx.HTTPError`` nor ``OSError``. It is broad because
#: ``QmdRecycleFailed`` *is* a ``RuntimeError`` — the ``raise`` sits below
#: the guarded block, and ``tests/unit/qmd/test_daemon_recycle.py`` pins
#: that shape.
_RECYCLE_PROBE_ERRORS = (httpx.HTTPError, mcp.MCPError, OSError, RuntimeError)


async def recycle_qmd_daemon(
    *,
    scope: "_Path",
    daemon_url: str,
    ready_timeout: float = 30.0,
    poll_interval: float = 0.5,
) -> "QmdState":
    """Restarts qmd's daemon; polls list_tools() until it serves or budget expires.

    Reaps first (idempotent when not running), then spawns. The probe is a
    ``fastmcp.Client.list_tools()`` call against ``daemon_url``, proving the
    JSON-RPC session is alive rather than that the daemon is warm. The
    worst-case bound is a per-configuration table, not one figure — see
    AGENTS.md and ``tests/integration/test_qmd_recycle_ceiling.py``.

    Args:
        scope: the LIES scope to record in the sidecar. It does not
            select the daemon's index — see :func:`_canonical_scope`.
        daemon_url: the URL to probe, and the source of the ``--port`` the
            spawn must use (:func:`daemon_port_from_url`).

    Raises:
        QmdRecycleFailed: reap, spawn and probe never served within
            ``ready_timeout``. The only exception this function raises;
            the probe's own failures are absorbed into the retry loop.
    """
    _log.debug("recycle_qmd_daemon: reaping")
    _reap_qmd_daemon()
    _log.debug("recycle_qmd_daemon: spawning")
    # Spawn onto the port being probed, or the probe waits out its budget.
    _spawn_qmd_daemon(port=daemon_port_from_url(daemon_url))
    _log.debug("recycle_qmd_daemon: writing sidecar for %s", scope)
    write_sidecar_scope(scope)

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
        except _RECYCLE_PROBE_ERRORS as exc:
            _log.debug("recycle_qmd_daemon: probe failed (%s); retrying", exc)
            # The state is only read for the final ``QmdRecycleFailed``.
            last_state = qmd_daemon_state()
            await asyncio.sleep(poll_interval)
    raise QmdRecycleFailed(ready_timeout, last_state)
