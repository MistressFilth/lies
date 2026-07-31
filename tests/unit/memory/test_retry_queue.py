"""Unit tests for the EnrichmentQueue."""
from __future__ import annotations

import pytest

from lies.memory.enricher import MemoryEnricherDeps
from lies.memory.models import (
    MemoryPlan,
    MemoryReceipt,
    OperationKind,
    PageCreate,
    PageReference,
    WikiCommitFailed,
    WikiLockBusy,
    WikiPlanInvalid,
    WikiWriteConflict,
)
from lies.memory.retry import DrainResult, EnrichmentQueue, PendingRetry


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


def test_drain_empty_queue_returns_zero_result() -> None:
    queue = EnrichmentQueue()
    result = queue.drain(
        enrich_fn=lambda d: MemoryPlan(operations=[], rationale="", evidence=[]),
        apply_fn=lambda p: MemoryReceipt(),
    )
    assert result == DrainResult()


def test_drain_applies_plan_and_drops_item() -> None:
    queue = EnrichmentQueue()
    queue.enqueue(_deps(), "WikiLockBusy: held", turn=1)

    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/x.md",
                content="---\ntitle: X\ntype: concept\n---\n# X\n",
                evidence=["page-1"],
            )
        ],
        rationale="new",
        evidence=["page-1"],
    )
    receipt = MemoryReceipt(
        changed_pages=[
            PageReference(path="concepts/x.md", collection_id="wiki", op=OperationKind.CREATE)
        ],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )

    def enrich_fn(_deps: MemoryEnricherDeps) -> MemoryPlan:
        return plan

    def apply_fn(applied_plan: MemoryPlan) -> MemoryReceipt:
        assert applied_plan is plan
        return receipt

    result = queue.drain(enrich_fn=enrich_fn, apply_fn=apply_fn)
    assert result.applied == receipt.changed_pages
    assert result.deferred == []
    assert result.still_queued == 0
    assert len(queue) == 0


def test_drain_drops_noop_plan_silently() -> None:
    queue = EnrichmentQueue()
    queue.enqueue(_deps(), "WikiLockBusy: held", turn=1)

    def enrich_fn(_deps: MemoryEnricherDeps) -> MemoryPlan:
        return MemoryPlan(operations=[], rationale="nothing", evidence=[])

    def apply_fn(_p: MemoryPlan) -> MemoryReceipt:
        pytest.fail("apply_fn should not be called for a noop plan")

    result = queue.drain(enrich_fn=enrich_fn, apply_fn=apply_fn)
    assert result == DrainResult()
    assert len(queue) == 0


def _plan_with_create() -> MemoryPlan:
    return MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/x.md",
                content="---\ntitle: X\ntype: concept\n---\n# X\n",
                evidence=["page-1"],
            )
        ],
        rationale="new",
        evidence=["page-1"],
    )


def test_drain_requeues_on_wiki_lock_busy() -> None:
    queue = EnrichmentQueue(max_attempts=3)
    queue.enqueue(_deps(), "WikiLockBusy: prior", turn=1)

    def enrich_fn(_d: MemoryEnricherDeps) -> MemoryPlan:
        return _plan_with_create()

    def apply_fn(_p: MemoryPlan) -> MemoryReceipt:
        raise WikiLockBusy("wiki memory lock is held by another process")

    result = queue.drain(enrich_fn=enrich_fn, apply_fn=apply_fn)
    assert result.deferred == []
    assert result.still_queued == 1
    assert result.applied == []
    assert len(queue) == 1
    item = queue._items[0]  # type: ignore[attr-defined]
    assert item.attempts == 1
    assert item.last_reason.startswith("WikiLockBusy:")


def test_drain_requeues_on_wiki_write_conflict() -> None:
    queue = EnrichmentQueue(max_attempts=3)
    queue.enqueue(_deps(), "WikiLockBusy: prior", turn=1)

    def apply_fn(_p: MemoryPlan) -> MemoryReceipt:
        raise WikiWriteConflict("hash mismatch for x.md")

    result = queue.drain(enrich_fn=lambda d: _plan_with_create(), apply_fn=apply_fn)
    assert result.still_queued == 1
    assert queue._items[0].last_reason.startswith("WikiWriteConflict:")  # type: ignore[attr-defined]


def test_drain_requeues_on_wiki_commit_failed() -> None:
    queue = EnrichmentQueue(max_attempts=3)
    queue.enqueue(_deps(), "WikiLockBusy: prior", turn=1)

    def apply_fn(_p: MemoryPlan) -> MemoryReceipt:
        raise WikiCommitFailed("commit failed: non-fast-forward")

    result = queue.drain(enrich_fn=lambda d: _plan_with_create(), apply_fn=apply_fn)
    assert result.still_queued == 1
    assert queue._items[0].last_reason.startswith("WikiCommitFailed:")  # type: ignore[attr-defined]


def test_drain_defers_after_max_attempts() -> None:
    queue = EnrichmentQueue(max_attempts=3)
    # Simulate an item that already has 2 attempts
    queue._items.append(  # type: ignore[attr-defined]
        PendingRetry(
            deps=_deps(),
            attempts=2,
            last_reason="WikiLockBusy: prior",
            enqueued_at_turn=1,
        )
    )

    def apply_fn(_p: MemoryPlan) -> MemoryReceipt:
        raise WikiLockBusy("still held")

    result = queue.drain(enrich_fn=lambda d: _plan_with_create(), apply_fn=apply_fn)
    assert result.still_queued == 0
    assert result.deferred == ["WikiLockBusy: still held"]
    assert len(queue) == 0


def test_drain_defers_on_terminal_apply_exception() -> None:
    queue = EnrichmentQueue()
    queue.enqueue(_deps(), "WikiLockBusy: prior", turn=1)

    def apply_fn(_p: MemoryPlan) -> MemoryReceipt:
        raise WikiPlanInvalid("page already exists; use UPDATE or APPEND", path="concepts/x.md")

    result = queue.drain(enrich_fn=lambda d: _plan_with_create(), apply_fn=apply_fn)
    assert result.still_queued == 0
    assert result.deferred == ["WikiPlanInvalid: page already exists; use UPDATE or APPEND"]
    assert len(queue) == 0


def test_drain_defers_on_enrich_fn_exception() -> None:
    queue = EnrichmentQueue()
    queue.enqueue(_deps(), "WikiLockBusy: prior", turn=1)

    def enrich_fn(_d: MemoryEnricherDeps) -> MemoryPlan:
        raise RuntimeError("model unavailable")

    result = queue.drain(enrich_fn=enrich_fn, apply_fn=lambda p: MemoryReceipt())
    assert result.still_queued == 0
    assert result.deferred == ["enricher_crashed: RuntimeError: model unavailable"]
    assert len(queue) == 0


def test_format_receipt_lines_empty_when_no_deferred() -> None:
    queue = EnrichmentQueue()
    queue.enqueue(_deps(), "WikiLockBusy: held", turn=1)
    assert queue.format_receipt_lines() == []


def test_format_receipt_lines_lists_deferred_reasons() -> None:
    queue = EnrichmentQueue(max_attempts=3)
    # Item already at attempts=2; one more transient failure defers it.
    queue._items.append(  # type: ignore[attr-defined]
        PendingRetry(
            deps=_deps(),
            attempts=2,
            last_reason="WikiLockBusy: prior",
            enqueued_at_turn=1,
        )
    )

    def apply_fn(_p: MemoryPlan) -> MemoryReceipt:
        raise WikiLockBusy("still held")

    queue.drain(enrich_fn=lambda d: _plan_with_create(), apply_fn=apply_fn)
    lines = queue.format_receipt_lines()
    assert lines == ["(memory: deferred after 3 attempts — WikiLockBusy: still held)"]


def test_drain_preserves_fifo_ordering() -> None:
    queue = EnrichmentQueue()
    queue.enqueue(_deps(answer="first"), "WikiLockBusy: first", turn=1)
    queue.enqueue(_deps(answer="second"), "WikiLockBusy: second", turn=2)

    seen_answers: list[str] = []

    def enrich_fn(d: MemoryEnricherDeps) -> MemoryPlan:
        seen_answers.append(d.answer)
        return _plan_with_create()

    queue.drain(
        enrich_fn=enrich_fn,
        apply_fn=lambda p: MemoryReceipt(),
    )
    assert seen_answers == ["first", "second"]
