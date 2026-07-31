"""In-process retry queue for transient wiki-memory persistence failures.

When ``WikiMemoryService.apply_plan`` raises a transient error
(``WikiLockBusy``, ``WikiWriteConflict``, ``WikiCommitFailed``),
``EnrichmentQueue`` captures the evidence envelope and replays it on
the next turn. Per-session, in-memory only. No daemon, no disk
persistence.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from lies.memory.enricher import MemoryEnricherDeps
from lies.memory.models import MemoryPlan, MemoryReceipt, PageReference


@dataclass(frozen=True)
class PendingRetry:
    """A queued evidence envelope waiting for its next replay attempt."""

    deps: MemoryEnricherDeps
    attempts: int = 0
    last_reason: str = ""
    enqueued_at_turn: int = 0


@dataclass(frozen=True)
class DrainResult:
    """The outcome of one drain pass over the queue."""

    applied: list[PageReference] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    still_queued: int = 0


# Functions injected by the orchestrator at drain time. Decoupling lets the
# queue be unit-tested without a live enricher agent or wiki.
EnrichFn = Callable[[MemoryEnricherDeps], MemoryPlan]
ApplyFn = Callable[[MemoryPlan], MemoryReceipt]


# Exceptions that warrant another attempt instead of a terminal receipt.
TRANSIENT_PERSISTENCE_ERRORS: tuple[type[BaseException], ...] = (
    # Filled in by Task 3; placeholder so the type is importable now.
    Exception,  # narrowed in Task 3
)


class EnrichmentQueue:
    """Per-session FIFO of evidence envelopes awaiting replay."""

    def __init__(self, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        self._items: deque[PendingRetry] = deque()
        self._max_attempts = max_attempts

    def enqueue(
        self,
        deps: MemoryEnricherDeps,
        reason: str,
        turn: int,
    ) -> None:
        """Append a new pending retry with ``attempts=0``."""
        self._items.append(
            PendingRetry(
                deps=deps,
                attempts=0,
                last_reason=reason,
                enqueued_at_turn=turn,
            )
        )

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[PendingRetry]:  # pragma: no cover - introspection
        return iter(self._items)