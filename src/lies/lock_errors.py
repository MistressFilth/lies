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

    **The budget belongs to the raiser, not to this class.** Two paths
    share the flock and they wait for different lengths: the
    :func:`~lies.qmd.lock.with_qmd_lock` CLI decorator polls for 30 s,
    and the daemon query path
    (:func:`~lies.qmd.lock.qmd_lock_async`) polls for 120 s,
    overridable per call with ``LIES_QMD_QUERY_LOCK_WAIT``. So there is
    no single number to read off this docstring — read ``max_s`` off the
    instance, which every production raise site populates.

    Distinct from WikiLockBusy because (a) the qmd flock is site-wide, not
    per-wiki; (b) the operator recovery is to wait and retry the operation
    (concurrent CLI subprocesses are expected transient contention), not
    to run ``lies flock qmd force-repair``.

    (b) applies to the query path too, for a sharper reason: the daemon
    answered nothing because nothing was dispatched to it, so there is no
    daemon state to recycle and no ``mcp.log`` line to read. Retry is the
    remedy there; recycling is the wrong one and costs a model reload.

    Attributes:
        holder_pid: PID of the live holder, when known. ``None`` if unknown
            (e.g., the contention pre-dates the heartbeat envelope).
        waited_s: Wall-clock seconds spent polling before raising.
        max_s: The budget *that raiser* waited for, in seconds — read it
            off the instance rather than assuming a number. Every
            production raise site passes it explicitly: both raisers in
            :mod:`lies.qmd.lock` pass their own ``retry_budget_s``, and
            :meth:`~lies.library.writer.LibraryWriter.commit` forwards
            the value it caught. The ``30.0`` in ``__init__`` is a default
            for hand-built instances and matches no production raise site.
        lock_path: The inode the wait was spent on, when the raise site
            knows it. **Every production raise site passes it
            explicitly**: both raisers in :mod:`lies.qmd.lock` have the
            path in hand because the triad is resolved once per
            acquisition, and :meth:`~lies.library.writer.LibraryWriter.commit`
            forwards the value it caught. ``None`` only for a hand-built
            instance in a test, or a re-wrapping caller that has nothing
            to forward — which is why the docstring here cannot promise
            the path is always present. What it can promise is that no
            *production* site drops it, and
            ``tests/unit/qmd/test_qmd_lockbusy_sites.py`` asserts that.
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
            # The path is named because the flock is *resolvable*: it is
            # the one operator step that tells a contended host from a
            # broken one. A caller that queued and gave up needs to be
            # able to go and look at the inode, and "waited 120s, max
            # 120s" on its own does not say which one. `LIES_QMD_LOCK_PATH`
            # overrides it, so the path in the message is the only record
            # of *which* inode was contended.
            where = f" on {self.lock_path}" if self.lock_path else ""
            message = (
                f"qmd flock contention{where}: holder PID {holder_pid}, "
                f"waited {waited_s}s, max {max_s}s"
            )
        super().__init__(message)
