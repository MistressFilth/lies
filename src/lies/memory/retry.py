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
from lies.memory.models import (
    MemoryPlan,
    MemoryReceipt,
    PageReference,
    WikiCommitFailed,
    WikiLockBusy,
    WikiWriteConflict,
)


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
    WikiLockBusy,
    WikiWriteConflict,
    WikiCommitFailed,
)


class EnrichmentQueue:
    """Per-session FIFO of evidence envelopes awaiting replay."""

    def __init__(self, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        self._items: deque[PendingRetry] = deque()
        self._max_attempts = max_attempts
        self._last_deferred: list[str] = []  # set by drain(), consumed by format_receipt_lines()

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

    def drain(
        self,
        *,
        enrich_fn: EnrichFn,
        apply_fn: ApplyFn,
    ) -> DrainResult:
        """Walk the FIFO; apply each item; return the aggregate result.

        - enrich_fn failure → item moves to ``deferred`` (terminal).
        - plan.is_noop() → item drops silently.
        - apply_fn transient persistence error → re-queue at tail,
          unless ``attempts + 1 >= max_attempts`` (then ``deferred``).
        - apply_fn any other error → item moves to ``deferred``
          (terminal; never retried).
        - apply_fn success → item drops; changed_pages append to
          ``applied``.
        """
        from lies.memory.models import (
            WikiCommitFailed,
            WikiLockBusy,
            WikiWriteConflict,
        )
        applied: list[PageReference] = []
        deferred: list[str] = []
        for item in list(self._items):
            try:
                plan = enrich_fn(item.deps)
            except Exception as exc:  # noqa: BLE001 - terminal
                self._items.remove(item)
                deferred.append(f"enricher_crashed: {type(exc).__name__}: {exc!s}")
                continue
            if plan.is_noop():
                self._items.remove(item)
                continue
            try:
                receipt = apply_fn(plan)
            except (WikiLockBusy, WikiWriteConflict, WikiCommitFailed) as exc:
                self._items.remove(item)
                reason = f"{type(exc).__name__}: {exc!s}"
                if item.attempts + 1 >= self._max_attempts:
                    deferred.append(reason)
                else:
                    self._items.append(
                        PendingRetry(
                            deps=item.deps,
                            attempts=item.attempts + 1,
                            last_reason=reason,
                            enqueued_at_turn=item.enqueued_at_turn,
                        )
                    )
                continue
            except Exception as exc:  # noqa: BLE001 - non-transient: terminal
                self._items.remove(item)
                deferred.append(f"{type(exc).__name__}: {exc!s}")
                continue
            self._items.remove(item)
            applied.extend(receipt.changed_pages)
        self._last_deferred = deferred
        return DrainResult(applied=applied, deferred=deferred, still_queued=len(self._items))

    def format_receipt_lines(self) -> list[str]:
        """Return one-line strings for items deferred by the last ``drain``.

        Reads the most-recent ``deferred`` list captured on this queue.
        Use an overwrite-on-drain pattern: ``drain`` populates
        ``_last_deferred`` on every call; a successful drain overwrites
        the list with ``[]``, so subsequent reads return no lines.
        Callers format the lines on the next turn.

        Returns ``[]`` if nothing was deferred.
        """
        if not self._last_deferred:
            return []
        return [
            f"(memory: deferred after {self._max_attempts} attempts — {reason})"
            for reason in self._last_deferred
        ]
