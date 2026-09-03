"""Re-export of memory error classes for `from lies.memory.errors import ...`.

Keeps the historical import path (``lies.memory.errors``) stable for
callers that prefer to import typed exceptions alongside their related
models. The authoritative definitions live in :mod:`lies.memory.models`
and :mod:`lies.lock_errors`; this module only re-exports them.
"""

from __future__ import annotations

from lies.lock_errors import WikiLockBusy
from lies.memory.models import (
    WikiCommitFailed,
    WikiMemoryError,
    WikiPageNotFound,
    WikiPlanInvalid,
    WikiSearchUnavailable,
    WikiWriteConflict,
)

__all__ = [
    "WikiCommitFailed",
    "WikiLockBusy",
    "WikiMemoryError",
    "WikiPageNotFound",
    "WikiPlanInvalid",
    "WikiSearchUnavailable",
    "WikiWriteConflict",
]
