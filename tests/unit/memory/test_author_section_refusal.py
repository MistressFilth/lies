"""Writer refusal tests for F17 section contract.

``build_author_plan`` short-circuits with ``_SectionRefusal`` when the body
lacks required headings; ``file_back_author`` / ``file_back_synthesis``
surface the refusal as an errors-as-value ``MemoryReceipt``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lies.memory.models import MemoryPlan, MemoryReceipt
from lies.orchestrator import Orchestrator
from lies.page import build_author_plan
from lies.page.author import _SectionRefusal
from lies.query.citation import Citation
from lies.query.models import SynthesizedAnswer
from lies.schema.sections import SectionContract


@pytest.fixture
def contract() -> SectionContract:
    return SectionContract(
        entity=["## Overview", "## Description", "## References"],
        synthesis=["## Thesis", "## Evidence", "## Open Questions"],
    )


def test_build_author_plan_refuses_missing_sections(contract: SectionContract) -> None:
    """Body missing Description + References → refusal, not plan."""
    body = "## Overview\n\nx\n"
    result = build_author_plan(
        type="entity",
        collection="default",
        slug="widget",
        title="Widget",
        body=body,
        derived_from=[],
        tags=[],
        sources=[],
        exists=lambda _r: False,
        section_contract=contract,
    )
    assert isinstance(result, _SectionRefusal)
    assert "## Description" in result.error
    assert "## References" in result.error
    assert result.page_type == "entity"
    assert result.slug == "widget"
    assert result.title == "Widget"


def test_build_author_plan_passes_when_complete(contract: SectionContract) -> None:
    """All required sections present → plan built normally."""
    body = "## Overview\n\nx\n\n## Description\n\nx\n\n## References\n\nx\n"
    plan = build_author_plan(
        type="entity",
        collection="default",
        slug="widget",
        title="Widget",
        body=body,
        derived_from=[],
        tags=[],
        sources=[],
        exists=lambda _r: False,
        section_contract=contract,
    )
    assert not isinstance(plan, _SectionRefusal)
    assert isinstance(plan, MemoryPlan)
    assert plan.operations


@pytest.fixture
def orch_with_magic_memory():
    """Stub orchestrator with MagicMock wiki + memory service."""
    wiki = MagicMock()
    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
    orch.wiki = wiki
    orch._memory_service = MagicMock()
    return orch


def test_file_back_author_refuses_when_plan_carries_refusal(
    orch_with_magic_memory: Orchestrator,
) -> None:
    """Defensive seam: a ``_SectionRefusal`` reaching ``file_back_author`` surfaces as an errors-as-value ``MemoryReceipt`` rather than crashing."""
    refusal = _SectionRefusal(
        error="missing required section(s) for synthesis: ## Evidence, ## Open Questions",
        page_type="synthesis",
        slug="what-is-x",
        title="What is X",
    )
    receipt = orch_with_magic_memory.file_back_author(refusal)
    assert receipt.changed_pages == []
    assert receipt.errors
    joined = " ".join(receipt.errors)
    assert "## Evidence" in joined
    assert "## Open Questions" in joined
    orch_with_magic_memory._memory_service.apply_plan.assert_not_called()


@pytest.fixture
def orch_with_real_section_contract(contract, tmp_path):
    """Stub orchestrator whose wiki carries the real contract — exercises the production wiring ``self.wiki.section_contract`` -> ``build_author_plan``."""
    wiki = SimpleNamespace(section_contract=contract)
    wiki.wiki_dir = tmp_path
    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
    orch.wiki = wiki
    orch._memory_service = MagicMock()
    orch._memory_service.current_state = MagicMock(return_value=("f" * 64, ""))
    empty = MemoryReceipt(
        changed_pages=[], deferred=[], fallback_used=False, fallback_reason="", errors=[]
    )
    orch._memory_service.apply_plan = MagicMock(return_value=empty)
    return orch


def test_file_back_synthesis_refuses_missing_required_sections(
    orch_with_real_section_contract: Orchestrator,
) -> None:
    """Task 3 coverage pin: synthesis body lacking required headings short-circuits to a refusal receipt — not a silently-written page."""
    answer = SynthesizedAnswer(
        answer="Some prose that does not carry any of the required synthesis headings.",
        citations=[],
        pages_read=[Citation(path="claude-code/concepts/hooks", source="wiki")],
        should_file=True,
        question="What is a hook?",
    )
    receipt = orch_with_real_section_contract.file_back_synthesis(answer, collection="claude-code")
    assert receipt.changed_pages == []
    assert receipt.errors
    joined = " ".join(receipt.errors)
    assert "## Thesis" in joined
    assert "## Evidence" in joined
    assert "## Open Questions" in joined
    orch_with_real_section_contract._memory_service.apply_plan.assert_not_called()
