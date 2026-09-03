"""Tests for Orchestrator.file_back_synthesis."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from lies.memory.errors import WikiPlanInvalid
from lies.memory.models import (
    MemoryReceipt,
    PageReference,
    WikiLockBusy,
    OperationKind,
)
from lies.orchestrator import Orchestrator
from lies.query.models import SynthesizedAnswer


def _answer() -> SynthesizedAnswer:
    return SynthesizedAnswer(
        answer="A hook intercepts events at fixed points in the agent's lifecycle.",
        pages_read=["claude-code/concepts/hooks", "claude-code/concepts/skills"],
        should_file=True,
    )


def test_file_back_synthesis_success(tmp_path: Path) -> None:
    wiki_dir = tmp_path
    wiki = MagicMock()
    wiki.wiki_dir = wiki_dir
    memory_service = MagicMock()
    receipt = MemoryReceipt(
        changed_pages=[
            PageReference(
                path="claude-code/synthesis/x.md",
                collection_id="claude-code",
                op=OperationKind.CREATE,
            )
        ],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )
    memory_service.apply_plan.return_value = receipt

    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
        orch.wiki = wiki
        orch._memory_service = memory_service

    ans = _answer()
    out = orch.file_back_synthesis(ans, collection="claude-code")

    assert out is receipt
    assert memory_service.apply_plan.call_count == 1


def test_file_back_synthesis_retries_three_times_on_lock_busy(tmp_path: Path) -> None:
    wiki = MagicMock()
    wiki.wiki_dir = tmp_path
    memory_service = MagicMock()
    memory_service.apply_plan.side_effect = WikiLockBusy("busy")

    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
        orch.wiki = wiki
        orch._memory_service = memory_service

    out = orch.file_back_synthesis(_answer(), collection="claude-code")

    assert memory_service.apply_plan.call_count == 3
    assert out.errors == ["file_back_failed_after_3_attempts: WikiLockBusy: busy"]
    assert out.changed_pages == []


def test_file_back_synthesis_recovers_after_one_retry(tmp_path: Path) -> None:
    wiki = MagicMock()
    wiki.wiki_dir = tmp_path
    memory_service = MagicMock()
    receipt = MemoryReceipt(
        changed_pages=[
            PageReference(path="x.md", collection_id="claude-code", op=OperationKind.CREATE)
        ],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )
    memory_service.apply_plan.side_effect = [WikiLockBusy("busy"), receipt]

    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
        orch.wiki = wiki
        orch._memory_service = memory_service

    out = orch.file_back_synthesis(_answer(), collection="claude-code")

    assert memory_service.apply_plan.call_count == 2
    assert out is receipt


def test_file_back_synthesis_does_not_retry_on_wikiplaninvalid_from_apply_plan(
    tmp_path: Path,
) -> None:
    """``WikiPlanInvalid`` raised by ``apply_plan`` falls to the catch-all.

    ``WikiPlanInvalid`` is not in the retryable-exception tuple
    (``WikiLockBusy``/``WikiWriteConflict``/``WikiCommitFailed``), so the
    inline retry loop does not retry: the first call propagates to the
    catch-all ``except Exception`` branch which returns a
    ``MemoryReceipt`` with the error stringified. The receipt carries
    no ``changed_pages``.
    """
    wiki = MagicMock()
    wiki.wiki_dir = tmp_path
    memory_service = MagicMock()
    memory_service.apply_plan.side_effect = WikiPlanInvalid("pages_read empty")

    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
        orch.wiki = wiki
        orch._memory_service = memory_service

    out = orch.file_back_synthesis(_answer(), collection="claude-code")

    assert memory_service.apply_plan.call_count == 1
    assert any("WikiPlanInvalid" in e for e in out.errors)
    assert out.changed_pages == []


def test_file_back_synthesis_does_not_retry_on_wikiplaninvalid_from_build(
    tmp_path: Path,
) -> None:
    """Build-time ``WikiPlanInvalid`` short-circuits before ``apply_plan``.

    ``build_synthesis_plan`` raises ``WikiPlanInvalid`` for plan
    validation failures (e.g. empty ``pages_read``); the orchestrator's
    surrounding ``try`` returns immediately with a
    ``MemoryReceipt(errors=["plan_invalid: ..."])`` and never invokes
    ``apply_plan``.
    """
    wiki = MagicMock()
    wiki.wiki_dir = tmp_path
    memory_service = MagicMock()

    with (
        patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None),
        patch(
            "lies.orchestrator.build_synthesis_plan",
            side_effect=WikiPlanInvalid("pages_read is empty; nothing to file"),
        ),
    ):
        orch = Orchestrator.__new__(Orchestrator)
        orch.wiki = wiki
        orch._memory_service = memory_service

        out = orch.file_back_synthesis(_answer(), collection="claude-code")

    assert memory_service.apply_plan.call_count == 0
    assert out == MemoryReceipt(
        changed_pages=[],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=["plan_invalid: pages_read is empty; nothing to file"],
    )
