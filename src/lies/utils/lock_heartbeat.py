"""Lock heartbeat + holder-PID file helpers.

Pair with :mod:`lies.utils.exclusive` — the lock primitive leaves
"who holds the flock" and "since when" to these helpers. Heartbeat is
a flat JSON dict so future expansion (memory size, build sha, etc.)
doesn't break readers.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Heartbeat:
    pid: int
    started_at: float
    scope: str = ""


@dataclass(frozen=True)
class AcquireResult:
    """Result of an ``acquire_create_lock`` attempt."""

    fd: int
    holder_pid: int | None = None
    holder_started_at: float | None = None
    status: Literal["acquired", "busy", "dead_reaped", "indeterminate"] = "acquired"


def write_owner_pid(path: Path, pid: int) -> None:
    """Writes ``pid`` as one int per line, atomically by rename.

    A reader gets either the old bytes or the new ones; ``write_text``
    truncates first, so a contender reading between truncate and write
    sees an empty file.

    The temp file is a sibling of the target (``os.replace`` is atomic
    only within one filesystem) and is created with ``open`` rather than
    ``mkstemp``, so it keeps the mode a direct write would have
    produced. No ``fsync``: the property bought is atomicity against a
    concurrent reader, not durability.
    """
    payload = str(int(pid))
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        # A failed write must not leave debris next to the pid file; the
        # next reader would treat it as a second, unparseable pid file.
        tmp.unlink(missing_ok=True)
        raise


def read_owner_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def write_heartbeat(path: Path, heartbeat: Heartbeat) -> None:
    path.write_text(json.dumps(asdict(heartbeat)), encoding="utf-8")


def read_heartbeat(path: Path) -> Heartbeat | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError, json.JSONDecodeError:
        return None
    try:
        return Heartbeat(
            pid=int(payload["pid"]),
            started_at=float(payload["started_at"]),
            scope=str(payload.get("scope", "")),
        )
    except KeyError, ValueError, TypeError:
        return None
