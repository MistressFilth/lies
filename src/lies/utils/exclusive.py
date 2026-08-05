"""Cross-process exclusive-create lock.

``acquire_create_lock`` closes the read-then-write race on any state
file: the caller reads state, decides, then writes — two processes can
both observe "free" and both win. Holding an ``O_CREAT | O_EXCL`` create
on a sibling path across that whole sequence makes the decision
single-winner, because the OS guarantees exactly one create succeeds.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def acquire_create_lock(path: Path, *, max_age_s: float) -> int | None:
    """Atomically create ``path``. Return the open fd on win, ``None`` on contention.

    The fd is left open so the OS holds the inode reference; the caller
    must pass it to :func:`release_create_lock`.

    Orphan recovery: if ``path`` exists but its mtime is older than
    ``max_age_s`` (a previous holder crashed before releasing), the
    orphan is unlinked and the create is retried once. Callers pick a
    window matching how long their guarded section can legitimately run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        if not _is_orphaned(path, max_age_s):
            return None
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        try:
            return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return None


def _is_orphaned(path: Path, max_age_s: float) -> bool:
    """Return True if ``path`` exists and is older than the recovery window."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    return (time.time() - stat.st_mtime) > max_age_s


def release_create_lock(path: Path, fd: int | None) -> None:
    """Close ``fd`` (if any) and unlink ``path``. Safe to call twice."""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        path.unlink()
    except FileNotFoundError:
        pass
