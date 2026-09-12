"""Tests for Orchestrator.file_back_author (sibling of file_back_synthesis).

Mirrors tests/unit/test_file_back_synthesis.py cases. F3 wrapper stays
in Task 7; this task only covers the new never-raises wrapper.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lies.memory.models import (
    MemoryPlan,
    MemoryReceipt,
    PageCreate,
    WikiCommitFailed,
    WikiLockBusy,
    WikiPlanInvalid,
    WikiWriteConflict,
)
from lies.orchestrator import Orchestrator


def _plan() -> MemoryPlan:
    return MemoryPlan(
        operations=[
            PageCreate(
                path="c/concepts/x.md",
                content="body",
                evidence=["c/x"],
                tag="author",
            )
        ],
        rationale="author",
        evidence=["c/x"],
    )


def _receipt_ok() -> MemoryReceipt:
    return MemoryReceipt(
        changed_pages=[],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )


@pytest.fixture
def orch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    """Bypass ``Orchestrator.__init__`` and stub ``_memory_service``.

    The wrapper only depends on ``self._memory_service.apply_plan`` and
    the typed-error imports, so we bypass the heavy ``_build`` (which
    constructs a real agent, registers sub-agents, etc.) and inject a
    ``MagicMock`` for the memory service.

    Also stubs ``time.sleep`` in the orchestrator module so the
    retry-backoff ``sleep(0.1)`` between attempts doesn't dominate the
    wall-clock for tests that exhaust all three retries.
    """
    wiki = MagicMock()
    wiki.wiki_dir = tmp_path
    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
    orch.wiki = wiki
    orch._memory_service = MagicMock(register_evidence=MagicMock())
    orch._memory_service.apply_plan = MagicMock(return_value=_receipt_ok())
    monkeypatch.setattr("lies.orchestrator.time.sleep", lambda *_a, **_kw: None)
    return orch


def test_register_evidence_called_before_apply_plan(orch: Orchestrator) -> None:
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="c/concepts/first.md",
                content="first",
                evidence=["c/first", "shared/ref"],
                tag="author",
            ),
            PageCreate(
                path="c/concepts/second.md",
                content="second",
                evidence=["c/second", "shared/ref"],
                tag="author",
            ),
        ],
        rationale="author",
        evidence=["plan-level/ref"],
    )
    events: list[str] = []

    def record_register(_references: set[str]) -> None:
        events.append("register")

    def record_apply(_plan: MemoryPlan) -> MemoryReceipt:
        events.append("apply")
        return _receipt_ok()

    orch._memory_service.register_evidence.side_effect = record_register
    orch._memory_service.apply_plan.side_effect = record_apply

    receipt = orch.file_back_author(plan)

    orch._memory_service.register_evidence.assert_called_once_with(
        {"c/first", "c/second", "shared/ref"}
    )
    assert events == ["register", "apply"]
    assert receipt.errors == []


def test_success_on_first_attempt(orch: Orchestrator) -> None:
    receipt = orch.file_back_author(_plan())
    assert receipt.errors == []
    assert orch._memory_service.apply_plan.call_count == 1


def test_transient_lock_busy_retries_3_times(orch: Orchestrator) -> None:
    orch._memory_service.apply_plan = MagicMock(side_effect=WikiLockBusy("flock held"))
    receipt = orch.file_back_author(_plan())
    assert orch._memory_service.apply_plan.call_count == 3
    assert receipt.changed_pages == []
    assert any("file_back_failed_after_3_attempts" in e for e in receipt.errors)


def test_transient_then_success(orch: Orchestrator) -> None:
    orch._memory_service.apply_plan = MagicMock(side_effect=[WikiLockBusy("x"), _receipt_ok()])
    receipt = orch.file_back_author(_plan())
    assert orch._memory_service.apply_plan.call_count == 2
    assert receipt.errors == []


def test_write_conflict_no_retry(orch: Orchestrator) -> None:
    """WikiWriteConflict is treated like the other transient errors here.

    ``file_back_synthesis`` includes ``WikiWriteConflict`` in the retry
    tuple (sha256 mismatch — same flock+commit envelope as a lock busy).
    Three attempts exhaust and the receipt surfaces the error.
    """
    orch._memory_service.apply_plan = MagicMock(side_effect=WikiWriteConflict("sha mismatch"))
    receipt = orch.file_back_author(_plan())
    assert orch._memory_service.apply_plan.call_count == 3
    assert any(
        "file_back_failed_after_3_attempts" in e or "WikiWriteConflict" in e for e in receipt.errors
    )


def test_unexpected_exception_does_not_raise(orch: Orchestrator) -> None:
    """An unexpected exception must never raise — wrapper returns a crash receipt."""
    orch._memory_service.apply_plan = MagicMock(side_effect=RuntimeError("boom"))
    # Never raises — returns crash receipt.
    receipt = orch.file_back_author(_plan())
    assert any("file_back_crashed" in e for e in receipt.errors)


def test_commit_failed_retries(orch: Orchestrator) -> None:
    orch._memory_service.apply_plan = MagicMock(side_effect=WikiCommitFailed("git error"))
    receipt = orch.file_back_author(_plan())
    assert orch._memory_service.apply_plan.call_count == 3
    assert any("file_back_failed_after_3_attempts" in e for e in receipt.errors)


def test_wikiplaninvalid_does_not_retry(orch: Orchestrator) -> None:
    """WikiPlanInvalid is permanent; no retry, falls to crash receipt."""
    orch._memory_service.apply_plan = MagicMock(side_effect=WikiPlanInvalid("bad plan"))
    receipt = orch.file_back_author(_plan())
    assert orch._memory_service.apply_plan.call_count == 1
    assert any("WikiPlanInvalid" in e for e in receipt.errors)
