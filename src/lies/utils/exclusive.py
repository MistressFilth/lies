"""Cross-process exclusive-create lock and gitignore guard.

Both primitives originally lived inline in ``etl/heartbeat.py`` and
``memory/service.py``. They are TOCTOU-sensitive, so exactly one
implementation is kept here and the original call sites delegate.

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


def ensure_gitignored(path: Path, *, wiki_root: Path) -> None:
    """Ensure ``path`` is listed in ``<wiki_root>/.gitignore``.

    Appends the wiki-relative path on its own line if no non-comment
    line already equals it. Creates ``.gitignore`` when absent.
    Idempotent — safe on every call.

    This closes a race window for lock files: without the entry,
    ``git stash push --include-untracked`` moves the lock file aside.
    A kernel flock survives on the orphaned inode, but a concurrent
    process can then create a new file at the same path and lock the
    new inode, reopening the race.
    """
    gitignore_path = wiki_root / ".gitignore"
    relative_line = path.relative_to(wiki_root).as_posix()

    existing = ""
    if gitignore_path.exists():
        try:
            existing = gitignore_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    if any(line.strip() == relative_line for line in existing.splitlines()):
        return

    if existing and not existing.endswith("\n"):
        existing += "\n"
    gitignore_path.write_text(existing + relative_line + "\n", encoding="utf-8")
