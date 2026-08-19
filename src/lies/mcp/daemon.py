"""Pidfile lifecycle for the detached LIES MCP daemon.

``server.py`` stays a pure FastMCP surface and ``cli.py`` stays thin
dispatch; this module owns everything about the daemon's on-disk
record and process lifecycle.

The daemon is per-wiki: its pidfile lives under that wiki's XDG
runtime directory (``$XDG_RUNTIME_DIR/lies/<wiki>/mcp.pid``), so two
wikis can each run one (the second passes ``--port``).

Ownership rule: only the parent that ran ``up`` writes the pidfile, and
only ``down`` clears it. The child never touches it. A crashed daemon
must leave the record behind so :func:`is_stale` can report honestly —
a self-unlinking child would make a crash indistinguishable from a
clean stop.
"""

from __future__ import annotations

import ipaddress
import os
import signal as signal_module
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from lies import __version__
from lies.utils.exclusive import acquire_create_lock, release_create_lock
from lies.wiki.wiki import Wiki

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8737
# FastMCP 3.4.5 mounts the streamable HTTP endpoint at "/mcp" (no
# trailing slash); earlier drafts of this module assumed "/mcp/". The
# bare path matches what `FastMCP.http_app()` resolves when no `path`
# argument is passed.
MCP_PATH = "/mcp"
CREATE_LOCK_MAX_AGE_S = 60.0


class DaemonError(Exception):
    """Base class for every daemon lifecycle failure."""


class DaemonAlreadyRunning(DaemonError):
    """A live daemon already owns this wiki.

    Carries the existing record so the caller can report its URL
    instead of re-deriving one.
    """

    def __init__(self, message: str, *, record: PidRecord) -> None:
        super().__init__(message)
        self.record = record


class DaemonBusy(DaemonError):
    """Another lifecycle operation holds the create-lock for this wiki."""


class NonLoopbackBind(DaemonError):
    """The daemon cannot bind remotely without an authentication layer."""


class PortUnavailable(DaemonError):
    """The requested bind address is occupied by an untracked process."""


class DaemonStartFailed(DaemonError):
    """The child exited, or never accepted a connection within the timeout."""


class DaemonStopFailed(DaemonError):
    """The daemon process survived SIGKILL."""


class PidRecord(BaseModel):
    """On-disk description of a running daemon.

    A record on disk means "this daemon accepted a connection at least
    once", not "we tried to start something" — the parent writes it
    only after the readiness gate passes.
    """

    pid: int
    host: str
    port: int
    transport: str
    started_at: datetime
    wiki_root: str
    version: str


def pid_path(wiki: Wiki) -> Path:
    return wiki.mcp_pid_path


def create_lock_path(wiki: Wiki) -> Path:
    return wiki.mcp_create_lock_path


def log_path(wiki: Wiki) -> Path:
    return wiki.mcp_log_path


def read_record(wiki: Wiki) -> PidRecord | None:
    """Return the record, or ``None`` when missing, unreadable, or corrupt.

    A truncated or schema-mismatched pidfile is treated as absent rather
    than raising. A daemon that crashed mid-write must not wedge every
    subsequent ``up``.
    """
    path = pid_path(wiki)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError, OSError:
        return None
    try:
        return PidRecord.model_validate_json(raw)
    except ValidationError:
        return None


def write_record(wiki: Wiki, rec: PidRecord) -> None:
    """Write the record atomically (temp file plus ``os.replace``)."""
    path = pid_path(wiki)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(rec.model_dump_json(), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def clear_record(wiki: Wiki) -> None:
    """Remove the record. Idempotent."""
    try:
        pid_path(wiki).unlink()
    except FileNotFoundError:
        pass


def process_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` exists.

    ``EPERM`` counts as alive: the process exists, we simply may not
    signal it.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _daemon_cmdline_matches(pid: int) -> bool | None:
    """Identify a spawned LIES daemon from its procfs command line.

    ``None`` means procfs could not be inspected, so callers must fall
    back to liveness-only behavior for portability.
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError, PermissionError, OSError:
        return None
    return b"lies.cli" in cmdline and b"_serve" in cmdline


def is_stale(rec: PidRecord) -> bool:
    """Return True when the record's process is dead or not this daemon."""
    if not process_alive(rec.pid):
        return True
    identity = _daemon_cmdline_matches(rec.pid)
    return identity is False


def daemon_url(rec: PidRecord) -> str:
    """Return the streamable-http URL an MCP host should register."""
    try:
        address = ipaddress.ip_address(rec.host)
    except ValueError:
        address = None
    host = f"[{rec.host}]" if address is not None and address.version == 6 else rec.host
    return f"http://{host}:{rec.port}{MCP_PATH}"


def require_loopback_host(host: str) -> None:
    """Reject bind hosts that can expose the unauthenticated daemon."""
    if host.lower() == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and address.is_loopback:
        return
    raise NonLoopbackBind(
        f"refusing non-loopback bind host {host!r}: the LIES MCP daemon has no authentication; "
        "put a reverse proxy in front if you need remote access"
    )


def port_free(host: str, port: int) -> bool:
    """Return True if ``(host, port)`` can be bound right now.

    Probed before spawning so a non-LIES process squatting the port
    surfaces as a clear error rather than a confusing readiness timeout.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    family = socket.AF_INET6 if address is not None and address.version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def tail_log(wiki: Wiki, lines: int = 20) -> list[str]:
    """Return the last ``lines`` lines of the daemon log, or ``[]``."""
    try:
        body = log_path(wiki).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError, OSError:
        return []
    return body.splitlines()[-lines:]


def _wait_until_accepting(
    host: str, port: int, proc: subprocess.Popen[bytes], timeout: float
) -> None:
    """Block until the child accepts a connection, or raise.

    Polls the child's exit status alongside the socket so an immediate
    crash fails fast instead of burning the whole timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            raise DaemonStartFailed(f"daemon exited with code {code} before accepting connections")
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise DaemonStartFailed(
        f"daemon did not accept a connection on {host}:{port} within {timeout:g}s"
    )


def _kill_now(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child and reap it, ignoring an already-dead process."""
    try:
        proc.kill()
    except ProcessLookupError, OSError:
        return
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def spawn_daemon(
    wiki: Wiki,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
) -> PidRecord:
    """Start a detached streamable-http daemon and return its record.

    Raises :class:`NonLoopbackBind` before any lifecycle state is touched,
    :class:`DaemonAlreadyRunning` when a live daemon owns this wiki,
    :class:`DaemonBusy` on create-lock contention, :class:`PortUnavailable`
    when the address is occupied, and :class:`DaemonStartFailed` when the
    child dies or never accepts a connection. The record is written only
    after the readiness gate passes, so ``up`` never reports success for a
    dead child.
    """
    require_loopback_host(host)
    lock = create_lock_path(wiki)
    lock_result = acquire_create_lock(lock, max_age_s=CREATE_LOCK_MAX_AGE_S)
    if lock_result is None:
        raise DaemonBusy(f"another lies mcp lifecycle operation is in progress: {lock}")
    fd = lock_result.fd
    try:
        existing = read_record(wiki)
        if existing is not None:
            if not is_stale(existing):
                raise DaemonAlreadyRunning(
                    f"lies mcp daemon already running at {daemon_url(existing)} "
                    f"(pid {existing.pid})",
                    record=existing,
                )
            clear_record(wiki)

        if not port_free(host, port):
            raise PortUnavailable(f"{host}:{port} is already in use")

        log = log_path(wiki)
        log.parent.mkdir(parents=True, exist_ok=True)

        with log.open("ab") as log_fd:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "lies.cli",
                    "mcp",
                    "_serve",
                    "--host",
                    host,
                    "--port",
                    str(port),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(wiki.data_root),
                env={**os.environ, "LIES_WIKI_NAME": wiki.name},
            )

        try:
            _wait_until_accepting(host, port, proc, timeout)
        except DaemonStartFailed:
            _kill_now(proc)
            raise

        rec = PidRecord(
            pid=proc.pid,
            host=host,
            port=port,
            transport="http",
            started_at=datetime.now(UTC),
            wiki_root=str(wiki.data_root),
            version=__version__,
        )
        write_record(wiki, rec)
        return rec
    finally:
        release_create_lock(lock, fd)


@dataclass(frozen=True)
class StopResult:
    """Outcome of :func:`stop_daemon`.

    ``action`` is ``"stopped"`` when a live process was signalled,
    ``"cleared_stale"`` when only a dead record was removed, and
    ``"none"`` when there was nothing to do.
    """

    action: str
    pid: int | None
    signal: str | None


@dataclass(frozen=True)
class StatusResult:
    """Outcome of :func:`daemon_status`."""

    running: bool
    record: PidRecord | None
    stale: bool
    url: str | None
    uptime_s: float | None
    log: Path


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` disappears. Return True if it did.

    ``os.kill(pid, 0)`` succeeds for zombies on Linux, so a process we
    signalled that exits cleanly lingers as a zombie until the parent
    reaps it. ``_pid_alive`` reads ``/proc/<pid>/stat`` to detect that
    state without reaping — :func:`process_alive` alone would spin
    forever on a zombie.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    """Return True when ``pid`` is still a live, non-zombie process.

    Reads ``/proc/<pid>/stat`` for the kernel's process-state field.
    State ``Z`` (zombie) is treated as dead; anything else is treated
    as live. When procfs is unreadable (missing, restricted, or this
    isn't Linux) the function falls back to :func:`process_alive` so
    the call still works — it just won't catch zombies in that case.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except FileNotFoundError, PermissionError, OSError:
        return process_alive(pid)
    end = raw.rfind(")")
    if end == -1 or end + 2 >= len(raw):
        return process_alive(pid)
    state = raw[end + 2 : end + 3]
    if not state:
        return process_alive(pid)
    return state != "Z"


def stop_daemon(wiki: Wiki, *, grace: float = 10.0) -> StopResult:
    """Stop the tracked daemon, escalating SIGTERM to SIGKILL.

    Only pidfile-tracked daemons are touched — never the stdio servers
    an MCP host spawned as its own children.

    Raises :class:`DaemonBusy` on create-lock contention and
    :class:`DaemonStopFailed` if the process survives SIGKILL. A missing
    or stale record is a successful no-op, not an error.
    """
    lock = create_lock_path(wiki)
    lock_result = acquire_create_lock(lock, max_age_s=CREATE_LOCK_MAX_AGE_S)
    if lock_result is None:
        raise DaemonBusy(f"another lies mcp lifecycle operation is in progress: {lock}")
    fd = lock_result.fd
    try:
        rec = read_record(wiki)
        if rec is None:
            return StopResult(action="none", pid=None, signal=None)
        if is_stale(rec):
            clear_record(wiki)
            return StopResult(action="cleared_stale", pid=rec.pid, signal=None)

        try:
            os.kill(rec.pid, signal_module.SIGTERM)
        except ProcessLookupError:
            # Exited between the staleness check and the signal.
            clear_record(wiki)
            return StopResult(action="cleared_stale", pid=rec.pid, signal=None)

        if _wait_for_exit(rec.pid, grace):
            clear_record(wiki)
            return StopResult(action="stopped", pid=rec.pid, signal="SIGTERM")

        try:
            os.kill(rec.pid, signal_module.SIGKILL)
        except ProcessLookupError:
            clear_record(wiki)
            return StopResult(action="stopped", pid=rec.pid, signal="SIGTERM")

        if not _wait_for_exit(rec.pid, 2.0):
            raise DaemonStopFailed(f"pid {rec.pid} survived SIGKILL")
        clear_record(wiki)
        return StopResult(action="stopped", pid=rec.pid, signal="SIGKILL")
    finally:
        release_create_lock(lock, fd)


def daemon_status(wiki: Wiki) -> StatusResult:
    """Report whether a tracked daemon is running for ``wiki``.

    Read-only: a stale record is reported as stale, not cleared. Only
    ``down`` mutates the record.
    """
    log = log_path(wiki)
    rec = read_record(wiki)
    if rec is None:
        return StatusResult(
            running=False, record=None, stale=False, url=None, uptime_s=None, log=log
        )
    if is_stale(rec):
        return StatusResult(running=False, record=rec, stale=True, url=None, uptime_s=None, log=log)
    raw_uptime = (datetime.now(UTC) - rec.started_at).total_seconds()
    # A hand-edited or clock-skewed record can carry a future
    # ``started_at``; clamp at zero rather than report a negative
    # duration that no caller can make sense of.
    uptime = max(0.0, raw_uptime)
    return StatusResult(
        running=True,
        record=rec,
        stale=False,
        url=daemon_url(rec),
        uptime_s=uptime,
        log=log,
    )
