"""Cross-process sync heartbeat for busy detection.

A single sync run writes ``$XDG_RUNTIME_DIR/lies/<wiki>/sync.lock``
with its PID, start time, and collection. A subsequent run reads the
file and treats it as busy unless the PID is dead or the heartbeat is
older than ``MAX_SYNC_AGE_S`` (stale-recovery for crashes).

Concurrency safety
------------------

``sync.lock`` is the observable state, but writing it is a
read-then-write of the same path — two processes can both observe
"no lock" and both win. To prevent that, :func:`acquire_create_lock`
takes an atomic ``O_CREAT | O_EXCL`` create on a sibling
``sync.lock.create``. Only the process that wins the create is allowed
to write the heartbeat; everyone else sees the busy state and bails
out (or waits, depending on policy). The lock file is unlinked in
:func:`release_create_lock`.

This mirrors the cross-process ``fcntl.flock`` pattern already used
by ``WikiMemoryService`` (which guards ``memory.lock``).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from lies.utils.exclusive import acquire_create_lock as _acquire_create_lock
from lies.utils.exclusive import release_create_lock as _release_create_lock
from lies.wiki.wiki import Wiki

MAX_SYNC_AGE_S = 3600
_MAX_CREATE_LOCK_AGE_S = MAX_SYNC_AGE_S


@dataclass(frozen=True)
class Heartbeat:
    pid: int
    started_at: float
    collection: str


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_create_lock(wiki: Wiki) -> int | None:
    """Atomically create ``$XDG_RUNTIME_DIR/lies/<wiki>/sync.lock.create``.

    Returns the fd on win. Thin wrapper over
    :func:`lies.utils.exclusive.acquire_create_lock`, supplying this
    module's path and recovery window. The window is intentionally
    the same as :data:`MAX_SYNC_AGE_S` so a single threshold governs
    the "this is stale" decision across the heartbeat and its
    create-lock.
    """
    return _acquire_create_lock(wiki.sync_create_lock_path, max_age_s=_MAX_CREATE_LOCK_AGE_S)


def release_create_lock(wiki: Wiki, fd: int | None) -> None:
    """Close the fd (if any) and unlink the lock file."""
    _release_create_lock(wiki.sync_create_lock_path, fd)


def write_heartbeat(wiki: Wiki, heartbeat: Heartbeat) -> None:
    p = wiki.sync_lock_path
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": heartbeat.pid,
        "started_at": heartbeat.started_at,
        "collection": heartbeat.collection,
    }
    p.write_text(json.dumps(payload), encoding="utf-8")


def read_heartbeat(wiki: Wiki) -> Heartbeat | None:
    p = wiki.sync_lock_path
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


def clear_heartbeat(wiki: Wiki) -> None:
    p = wiki.sync_lock_path
    if p.exists():
        p.unlink()


def heartbeat_is_stale(heartbeat: Heartbeat) -> bool:
    if not pid_alive(heartbeat.pid):
        return True
    age = time.time() - heartbeat.started_at
    return age > MAX_SYNC_AGE_S


def wait_until_free(wiki: Wiki, *, poll_interval_s: float = 1.0) -> None:
    while True:
        h = read_heartbeat(wiki)
        if h is None or heartbeat_is_stale(h):
            return
        time.sleep(poll_interval_s)
