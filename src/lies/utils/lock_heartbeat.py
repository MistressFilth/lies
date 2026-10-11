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
    """Result of an ``acquire_create_lock`` attempt.

    Carries both the file descriptor on success and the contender's
    pid + start time on contention so raise sites can construct
    operator-actionable messages. The new envelope is opt-in: callers
    that pass ``pid_path=None, state_json_path=None`` get the legacy
    ``int | None`` return.

    Status values:
      - ``"acquired"`` — fresh success; ``fd`` is valid.
      - ``"busy"`` — contended with a live contender; ``holder_pid`` and
        ``holder_started_at`` are populated from the existing files.
      - ``"dead_reaped"`` — the contendee was dead (or the heartbeat stale)
        and the reap-and-retry succeeded; ``fd`` is valid; holder info
        is ``None`` because the contendee is gone.
      - ``"indeterminate"`` — ``pid_alive_fn`` returned ``"indeterminate"``
        (EPERM on ``os.kill(pid, 0)``) and the heartbeat is older than
        the wall-clock window. The primitive did NOT reap; ``fd`` is
        the sentinel ``-1``; ``holder_pid`` + ``holder_started_at`` are
        populated so the raise site can emit ``WikiFlockIndeterminate``
        with operator-actionable details.
    """

    fd: int
    holder_pid: int | None = None
    holder_started_at: float | None = None
    status: Literal["acquired", "busy", "dead_reaped", "indeterminate"] = "acquired"


def write_owner_pid(path: Path, pid: int) -> None:
    """Write ``pid`` as one int per line (readable without JSON).

    Atomic by rename. ``Path.write_text`` truncates first, so a
    contender that reads between the truncate and the write sees an
    empty file, fails to parse it, and logs ``could not parse pid
    file`` (``exclusive._reap_if_stale``). That failure is benign and in
    the safe direction — the reader returns ``False``, so nobody reaps
    — but the query single-flight gate makes nine-way contention
    routine, so the warning now prints in normal operation and reads as
    a fault that is not there. Rename closes the window: a reader gets
    either the old bytes or the new ones.

    Two details that are not incidental. The temp file is created in
    the destination directory, because ``os.replace`` is atomic only
    within one filesystem and the parent is the only directory that
    guarantees it. And it is created with ``open`` rather than
    ``tempfile.mkstemp``, so it carries the same mode the direct write
    would have produced — ``mkstemp`` forces 0600 and would silently
    change who can read the holder PID.

    No ``fsync``: the property being bought here is atomicity against a
    concurrent reader, and that comes from ``rename(2)``, not from
    durability. The direct write this replaces had no ``fsync`` either,
    so this is not a regression, and a pid file that survived a crash
    half-upgraded is read in the safe direction — unparseable means
    "do not reap". The lock itself is the ``flock``, not this file.
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
