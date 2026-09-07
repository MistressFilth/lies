"""Regression pins for F3 file_back_synthesis behavior after refactor.

The wrapper public API (``Orchestrator.file_back_synthesis``) must produce
an identical MemoryReceipt shape once its body delegates to
``build_author_plan(type="synthesis", ...)`` + ``file_back_author``
(Task 7 of the f39-f12 bundle). The pre-refactor
``tests/unit/test_file_back_synthesis.py`` is deleted in this task;
the F3 plan-builder test suite likewise. Together these three tests
cover what was preserved:

1. Successful route through ``apply_plan`` exactly once.
2. The synthesized plan carries ``tag="synthesis"`` and lands under
   ``<collection>/synthesis/<slug>.md``.
3. ``WikiPlanInvalid`` from the plan builder short-circuits to a
   receipt whose ``errors`` field carries a ``"plan_invalid"`` prefix;
   ``apply_plan`` is never called.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lies.memory.models import MemoryReceipt
from lies.query.models import SynthesizedAnswer


def _orch(tmp_path: Path) -> "Orchestrator":  # noqa: F821
    """Bypass ``Orchestrator.__init__`` and stub ``_memory_service``.

    Same envelope as ``test_file_back_author.py`` — the wrapper only
    depends on ``self.wiki`` (for the ``exists`` closure) and
    ``self._memory_service`` (for ``apply_plan`` and
    ``current_state``), so we bypass the heavy ``_build`` (which
    constructs real agents and registers sub-agents) and inject
    ``MagicMock``s.
    """
    from lies.orchestrator import Orchestrator

    wiki = MagicMock()
    wiki.wiki_dir = tmp_path
    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
    orch.wiki = wiki
    orch._memory_service = MagicMock()
    orch._memory_service.apply_plan = MagicMock(
        return_value=MemoryReceipt(
            changed_pages=[],
            deferred=[],
            fallback_used=False,
            fallback_reason="",
            errors=[],
        )
    )
    return orch


@pytest.fixture
def orch(tmp_path: Path):
    return _orch(tmp_path)


def _answer() -> SynthesizedAnswer:
    return SynthesizedAnswer(
        answer="answer body",
        citations=[],
        pages_read=["claude-code/concepts/hooks"],
        should_file=True,
        question="What is a hook?",
    )


def test_file_back_synthesis_routes_to_apply_plan(orch):
    """Successful path calls ``apply_plan`` exactly once and returns the receipt."""
    receipt = orch.file_back_synthesis(_answer(), collection="claude-code")
    assert receipt.errors == []
    assert orch._memory_service.apply_plan.call_count == 1


def test_file_back_synthesis_tag_is_synthesis(orch):
    """The synthesized plan's tag must remain 'synthesis' (F3 invariant).

    The plan's tag is rendered into the git commit message and the
    ``wiki/log.md`` entry; downstream tooling relies on a stable
    ``"synthesis"`` tag for synthesis pages even after the refactor.
    The path must land under ``<collection>/synthesis/<slug>.md``.
    """
    captured: dict = {}

    def capture(plan):
        captured["tag"] = plan.operations[0].tag
        captured["path"] = plan.operations[0].path
        return MemoryReceipt(
            changed_pages=[],
            deferred=[],
            fallback_used=False,
            fallback_reason="",
            errors=[],
        )

    orch._memory_service.apply_plan = MagicMock(side_effect=capture)
    orch.file_back_synthesis(_answer(), collection="claude-code")
    assert captured["tag"] == "synthesis"
    assert captured["path"].startswith("claude-code/synthesis/")
    assert captured["path"].endswith(".md")


def test_file_back_synthesis_plan_invalid_returns_error_receipt(orch):
    """Empty ``pages_read`` -> ``WikiPlanInvalid`` -> ``plan_invalid`` receipt.

    F3 invariant: an answer with no evidence cannot synthesize a page,
    and the wrapper must never invoke ``apply_plan`` for it. The receipt
    carries the builder's exception text under the ``"plan_invalid"``
    error-prefix so the operator sees what failed without crashing.
    """
    bad = SynthesizedAnswer(
        answer="x",
        citations=[],
        pages_read=[],
        should_file=True,
        question="?",
    )
    receipt = orch.file_back_synthesis(bad, collection="claude-code")
    assert any("plan_invalid" in e for e in receipt.errors)
    assert orch._memory_service.apply_plan.call_count == 0
