"""Pidfile lifecycle for the detached LIES MCP daemon.

``server.py`` stays a pure FastMCP surface and ``cli.py`` stays thin
dispatch; this module owns everything about the daemon's on-disk
record and process lifecycle.

The daemon is per-wiki: its pidfile lives under that wiki's ``.lies/``,
so two wikis can each run one (the second passes ``--port``).

Ownership rule: only the parent that ran ``up`` writes the pidfile, and
only ``down`` clears it. The child never touches it. A crashed daemon
must leave the record behind so :func:`is_stale` can report honestly —
a self-unlinking child would make a crash indistinguishable from a
clean stop.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from lies import __version__
from lies.utils.exclusive import (
    acquire_create_lock,
    ensure_gitignored,
    release_create_lock,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8737
# FastMCP 3.4.5 mounts the streamable HTTP endpoint at "/mcp" (no
# trailing slash); earlier drafts of this module assumed "/mcp/". The
# bare path matches what `FastMCP.http_app()` resolves when no `path`
# argument is passed.
MCP_PATH = "/mcp"
CREATE_LOCK_MAX_AGE_S = 60.0

_PID_NAME = "mcp.pid"
_CREATE_LOCK_NAME = "mcp.pid.create"
_LOG_NAME = "mcp.log"


class DaemonError(Exception):
    """Base class for every daemon lifecycle failure."""


class DaemonAlreadyRunning(DaemonError):
    """A live daemon already owns this wiki root.

    Carries the existing record so the caller can report its URL
    instead of re-deriving one.
    """

    def __init__(self, message: str, *, record: PidRecord) -> None:
        super().__init__(message)
        self.record = record


class DaemonBusy(DaemonError):
    """Another lifecycle operation holds the create-lock for this wiki."""


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


def pid_path(wiki_root: Path) -> Path:
    return wiki_root / ".lies" / _PID_NAME


def create_lock_path(wiki_root: Path) -> Path:
    return wiki_root / ".lies" / _CREATE_LOCK_NAME


def log_path(wiki_root: Path) -> Path:
    return wiki_root / ".lies" / _LOG_NAME


def read_record(wiki_root: Path) -> PidRecord | None:
    """Return the record, or ``None`` when missing, unreadable, or corrupt.

    A truncated or schema-mismatched pidfile is treated as absent rather
    than raising. A daemon that crashed mid-write must not wedge every
    subsequent ``up``.
    """
    path = pid_path(wiki_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        return PidRecord.model_validate_json(raw)
    except ValidationError:
        return None


def write_record(wiki_root: Path, rec: PidRecord) -> None:
    """Write the record atomically (temp file plus ``os.replace``)."""
    path = pid_path(wiki_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(rec.model_dump_json(), encoding="utf-8")
    os.replace(tmp, path)


def clear_record(wiki_root: Path) -> None:
    """Remove the record. Idempotent."""
    try:
        pid_path(wiki_root).unlink()
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


def is_stale(rec: PidRecord) -> bool:
    """Return True when the record exists but its process does not."""
    return not process_alive(rec.pid)


def daemon_url(rec: PidRecord) -> str:
    """Return the streamable-http URL an MCP host should register."""
    return f"http://{rec.host}:{rec.port}{MCP_PATH}"


def port_free(host: str, port: int) -> bool:
    """Return True if ``(host, port)`` can be bound right now.

    Probed before spawning so a non-LIES process squatting the port
    surfaces as a clear error rather than a confusing readiness timeout.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def ensure_daemon_gitignored(wiki_root: Path) -> None:
    """Gitignore the pidfile, its create-lock, and the daemon log.

    Called on every ``up`` so wikis created before this feature pick the
    entries up too.
    """
    for path in (pid_path(wiki_root), create_lock_path(wiki_root), log_path(wiki_root)):
        ensure_gitignored(path, wiki_root=wiki_root)


def tail_log(wiki_root: Path, lines: int = 20) -> list[str]:
    """Return the last ``lines`` lines of the daemon log, or ``[]``."""
    try:
        body = log_path(wiki_root).read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
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
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def spawn_daemon(
    wiki_root: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
) -> PidRecord:
    """Start a detached streamable-http daemon and return its record.

    Raises :class:`DaemonAlreadyRunning` when a live daemon owns this
    wiki root, :class:`DaemonBusy` on create-lock contention,
    :class:`PortUnavailable` when the address is occupied, and
    :class:`DaemonStartFailed` when the child dies or never accepts a
    connection. The record is written only after the readiness gate
    passes, so ``up`` never reports success for a dead child.
    """
    lock = create_lock_path(wiki_root)
    fd = acquire_create_lock(lock, max_age_s=CREATE_LOCK_MAX_AGE_S)
    if fd is None:
        raise DaemonBusy(f"another lies mcp lifecycle operation is in progress: {lock}")
    try:
        existing = read_record(wiki_root)
        if existing is not None:
            if not is_stale(existing):
                raise DaemonAlreadyRunning(
                    f"daemon already running at {daemon_url(existing)} (pid {existing.pid})",
                    record=existing,
                )
            clear_record(wiki_root)

        if not port_free(host, port):
            raise PortUnavailable(f"{host}:{port} is already in use")

        ensure_daemon_gitignored(wiki_root)
        log = log_path(wiki_root)
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
                cwd=str(wiki_root),
                env={**os.environ, "LIES_WIKI_ROOT": str(wiki_root)},
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
            started_at=datetime.now(timezone.utc),
            wiki_root=str(wiki_root),
            version=__version__,
        )
        write_record(wiki_root, rec)
        return rec
    finally:
        release_create_lock(lock, fd)
