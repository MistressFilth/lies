"""Typed errors raised by memory-flock acquisition and recovery.

Three new subclasses live under a new ``WikiFlockError`` base, plus the
existing ``WikiLockBusy`` (re-exported here for completeness) is now a
subclass of the same base. Callers can catch the broad category with
``except WikiFlockError`` or the narrower specifics; both old
``except WikiLockBusy`` clauses and new ``except WikiFlockUnrepairable``
clauses work without churn.

Hierarchy note (v0.10.3): ``WikiLockBusy`` was re-parented away from
``WikiMemoryError`` to ``WikiFlockError``. Pre-v0.10.3, ``WikiLockBusy``
subclassed ``WikiMemoryError``; post-v0.10.3 it subclasses
``WikiFlockError``. Any caller using ``except WikiMemoryError`` to catch
memory-related errors will stop catching ``WikiLockBusy``; use
``except WikiFlockError`` or the specific subclass instead.
"""

from __future__ import annotations


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
    """Another process holds the site-wide qmd CLI flock; 30 s wait exhausted.

    Distinct from WikiLockBusy because (a) the qmd flock is site-wide, not
    per-wiki; (b) the operator recovery is to wait and retry the operation
    (concurrent CLI subprocesses are expected transient contention), not
    to run ``lies flock qmd force-repair``.

    Attributes:
        holder_pid: PID of the live holder, when known. ``None`` if unknown
            (e.g., the contention pre-dates the heartbeat envelope).
        waited_s: Wall-clock seconds spent polling before raising.
        max_s: Configured timeout (default ``30.0`` per the spec).
    """

    def __init__(
        self,
        holder_pid: int | None = None,
        *,
        waited_s: float | None = None,
        max_s: float = 30.0,
    ) -> None:
        self.holder_pid = holder_pid
        self.waited_s = waited_s
        self.max_s = max_s
        super().__init__(
            f"qmd flock contention: holder PID {holder_pid}, waited {waited_s}s, max {max_s}s"
        )
