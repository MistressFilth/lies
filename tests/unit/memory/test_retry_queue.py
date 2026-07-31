"""Unit tests for the EnrichmentQueue."""
from __future__ import annotations

from lies.memory.enricher import MemoryEnricherDeps
from lies.memory.retry import EnrichmentQueue, PendingRetry


def _deps(answer: str = "stub answer") -> MemoryEnricherDeps:
    return MemoryEnricherDeps(
        user_request="stub",
        answer=answer,
        pages_read=[],
        citations=[],
        evidence_text="",
        current_page_metadata={},
        active_schema="## Page types\n",
    )


def test_enqueue_increments_len() -> None:
    queue = EnrichmentQueue()
    assert len(queue) == 0
    queue.enqueue(_deps(), "WikiLockBusy: held", turn=1)
    assert len(queue) == 1
    queue.enqueue(_deps(), "WikiWriteConflict: stale", turn=2)
    assert len(queue) == 2


def test_pending_retry_carries_deps_and_metadata() -> None:
    queue = EnrichmentQueue()
    deps = _deps(answer="specific answer")
    queue.enqueue(deps, "WikiLockBusy: held", turn=42)
    pending = queue._items[0]  # type: ignore[attr-defined]
    assert isinstance(pending, PendingRetry)
    assert pending.deps.answer == "specific answer"
    assert pending.attempts == 0
    assert pending.last_reason == "WikiLockBusy: held"
    assert pending.enqueued_at_turn == 42