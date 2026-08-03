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
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

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
