"""Cross-process sync heartbeat for busy detection.

A single sync run writes ``<wiki>/.lies/sync.lock`` with its PID,
start time, and collection. A subsequent run reads the file and
treats it as busy unless the PID is dead or the heartbeat is older
than ``MAX_SYNC_AGE_S`` (stale-recovery for crashes).

Concurrency safety
------------------

``sync.lock`` is the observable state, but writing it is a
read-then-write of the same path — two processes can both observe
"no lock" and both win. To prevent that, :func:`acquire_create_lock`
takes an atomic ``O_CREAT | O_EXCL`` create on a sibling
``.lies/sync.lock.create``. Only the process that wins the create
is allowed to write the heartbeat; everyone else sees the busy
state and bails out (or waits, depending on policy). The lock file
is unlinked in :func:`release_create_lock`.

This mirrors the cross-process ``fcntl.flock`` pattern already used
by ``WikiMemoryService`` (which guards ``.lies/memory.lock``).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

MAX_SYNC_AGE_S = 3600
_HEARTBEAT_NAME = "sync.lock"
_CREATE_LOCK_NAME = "sync.lock.create"


@dataclass(frozen=True)
class Heartbeat:
    pid: int
    started_at: float
    collection: str


def _heartbeat_path(wiki_root: Path) -> Path:
    return wiki_root / ".lies" / _HEARTBEAT_NAME


def _create_lock_path(wiki_root: Path) -> Path:
    return wiki_root / ".lies" / _CREATE_LOCK_NAME


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_create_lock(wiki_root: Path) -> int | None:
    """Atomically create the sibling lock file. Returns the fd on win.

    On contention (the file already exists) returns ``None``. The fd
    is left open so the OS holds the inode reference; the caller is
    expected to close it via :func:`release_create_lock`.

    ``O_EXCL`` makes the create atomic: the OS guarantees exactly one
    process sees success. Without this, two processes can both
    observe "no lock" between read and write.
    """
    path = _create_lock_path(wiki_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return None
    return fd


def release_create_lock(wiki_root: Path, fd: int | None) -> None:
    """Close the fd (if any) and unlink the lock file."""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        _create_lock_path(wiki_root).unlink()
    except FileNotFoundError:
        pass


def write_heartbeat(wiki_root: Path, heartbeat: Heartbeat) -> None:
    p = _heartbeat_path(wiki_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": heartbeat.pid,
        "started_at": heartbeat.started_at,
        "collection": heartbeat.collection,
    }
    p.write_text(json.dumps(payload), encoding="utf-8")


def read_heartbeat(wiki_root: Path) -> Heartbeat | None:
    p = _heartbeat_path(wiki_root)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return Heartbeat(
        pid=int(payload["pid"]),
        started_at=float(payload["started_at"]),
        collection=str(payload["collection"]),
    )


def clear_heartbeat(wiki_root: Path) -> None:
    p = _heartbeat_path(wiki_root)
    if p.exists():
        p.unlink()


def heartbeat_is_stale(heartbeat: Heartbeat) -> bool:
    if not pid_alive(heartbeat.pid):
        return True
    age = time.time() - heartbeat.started_at
    return age > MAX_SYNC_AGE_S


def wait_until_free(wiki_root: Path, *, poll_interval_s: float = 1.0) -> None:
    while True:
        h = read_heartbeat(wiki_root)
        if h is None or heartbeat_is_stale(h):
            return
        time.sleep(poll_interval_s)
