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
