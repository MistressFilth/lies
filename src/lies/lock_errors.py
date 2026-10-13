"""Typed errors raised by memory-flock acquisition and recovery."""

from __future__ import annotations

from pathlib import Path


class WikiFlockError(Exception):
    """Base for memory-flock-specific errors."""


class WikiFlockStale(WikiFlockError):
    """Reap detected a stale (dead-PID) flock; auto-retry succeeded.

    Operators see this in WARN logs, not as an exception raised to
    callers. Reserved for explicit-recovery callers.
    """


class WikiFlockUnrepairable(WikiFlockError):
    """``force_repair=True`` could not break the lock; manual intervention required."""


class WikiFlockCorrupt(WikiFlockError):
    """State files present but malformed (unreadable .state.json, non-int .pid).

    Manual ``lies flock <name> force-repair`` is the only safe recovery.
    """


class WikiLockBusy(WikiFlockError):
    """Another process holds the wiki memory lock."""


class WikiFlockIndeterminate(WikiFlockError):
    """``acquire_create_lock`` could not determine live state of the contender.

    Raised when ``pid_alive_fn`` returns ``"indeterminate"`` (EPERM on
    ``os.kill(pid, 0)``) AND the heartbeat is older than the wall-clock
    window. The caller must run ``lies flock <name> force-repair`` to
    override; the primitive does not reap.
    """


class QmdLockBusy(WikiFlockError):
    """Another process holds the site-wide qmd flock past the wait budget.

    The budget belongs to the raiser, not to this class — 30 s on the CLI
    decorator, 120 s on the daemon query path, retunable per call with
    ``LIES_QMD_QUERY_LOCK_WAIT`` — so read ``max_s`` off the instance.
    ``holder_pid``, ``waited_s`` and ``lock_path`` are what let a raise site
    name the contention; each is ``None`` when unknown.

    Distinct from ``WikiLockBusy`` because the qmd flock is site-wide, not
    per-wiki, and the recovery is to wait and retry rather than
    ``lies flock qmd force-repair``. Nothing was dispatched, so there is no
    daemon state to recycle.
    """

    def __init__(
        self,
        holder_pid: int | None = None,
        *,
        waited_s: float | None = None,
        max_s: float = 30.0,
        lock_path: str | Path | None = None,
        message: str | None = None,
    ) -> None:
        self.holder_pid = holder_pid
        self.waited_s = waited_s
        self.max_s = max_s
        self.lock_path = str(lock_path) if lock_path is not None else None
        if message is None:
            # Naming the inode is the one operator step that tells a
            # contended host from a broken one.
            where = f" on {self.lock_path}" if self.lock_path else ""
            message = (
                f"qmd flock contention{where}: holder PID {holder_pid}, "
                f"waited {waited_s}s, max {max_s}s"
            )
        super().__init__(message)
