"""Writer refusal tests for F17 section contract.

When ``build_author_plan`` is called with a ``section_contract`` that
requires sections absent from the body, it short-circuits with a
``_SectionRefusal`` instead of producing a ``MemoryPlan``. The
orchestrator-level ``file_back_author`` defensively refuses the same
way so a refusal that arrives through another seam still surfaces as
an errors-as-value ``MemoryReceipt`` rather than crashing.

The refusal is internal — MCP and CLI surfaces translate it into
their own error envelopes in Tasks 4 and 5.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lies.memory.models import MemoryPlan, PageCreate
from lies.orchestrator import Orchestrator
from lies.page import build_author_plan
from lies.page.author import _SectionRefusal
from lies.schema.sections import SectionContract


# ---------- helpers ----------


def _exists_always_false(_rel: str) -> bool:
    return False


def _sha_lookup(_rel: str) -> str:
    return "f" * 64


@pytest.fixture
def contract() -> SectionContract:
    """Per-type required-section contract for entity + synthesis tests."""
    return SectionContract(
        entity=["## Overview", "## Description", "## References"],
        synthesis=["## Thesis", "## Evidence", "## Open Questions"],
    )


# ---------- build_author_plan refusal seam ----------


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
        exists=_exists_always_false,
        sha_lookup=_sha_lookup,
        section_contract=contract,
    )
    # Refusal surfaces as a _SectionRefusal with the missing headings named.
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
        exists=_exists_always_false,
        sha_lookup=_sha_lookup,
        section_contract=contract,
    )
    assert not isinstance(plan, _SectionRefusal)
    assert isinstance(plan, MemoryPlan)
    assert plan.operations  # plan built normally


def test_build_author_plan_no_contract_means_no_enforcement() -> None:
    """Default (None) section_contract skips enforcement entirely.

    Existing F39 callers don't pass ``section_contract``; their
    bodies don't carry the required headings. To avoid a behaviour
    change for those callers, ``None`` (the default) disables the
    refusal seam — the writer behaves as before. Tasks 4 and 5 wire
    the wiki-level contract through the MCP/CLI call sites.
    """
    body = "## Overview\n\nx\n"  # incomplete; would be refused with a contract
    plan = build_author_plan(
        type="entity",
        collection="default",
        slug="widget",
        title="Widget",
        body=body,
        derived_from=[],
        tags=[],
        sources=[],
        exists=_exists_always_false,
        sha_lookup=_sha_lookup,
        # section_contract omitted intentionally
    )
    assert isinstance(plan, MemoryPlan)
    assert not isinstance(plan, _SectionRefusal)


def test_build_author_plan_empty_contract_passes() -> None:
    """Empty contract (all lists empty) is a no-op — passes any body."""
    body = "anything goes"
    empty = SectionContract()
    plan = build_author_plan(
        type="entity",
        collection="c",
        slug="s",
        title="T",
        body=body,
        derived_from=[],
        tags=[],
        sources=[],
        exists=_exists_always_false,
        sha_lookup=_sha_lookup,
        section_contract=empty,
    )
    assert isinstance(plan, MemoryPlan)
    assert not isinstance(plan, _SectionRefusal)


def test_build_author_plan_unknown_type_skips_refusal(contract: SectionContract) -> None:
    """An unknown page_type (defensive case) yields no refusal.

    ``_missing_required_sections`` returns ``[]`` for unknown types
    (Task 1 helper contract). The author surface enforces the type
    membership separately via ``WikiPlanInvalid``; refusal stays
    silent here so the existing type guard remains the single source
    of truth for "bad type".
    """
    from lies.memory.models import WikiPlanInvalid

    with pytest.raises(WikiPlanInvalid, match="not in ALLOWED_PAGE_TYPES"):
        build_author_plan(
            type="widget",  # type: ignore[arg-type]
            collection="c",
            slug="s",
            title="T",
            body="b",
            derived_from=[],
            tags=[],
            sources=[],
            exists=_exists_always_false,
            sha_lookup=_sha_lookup,
            section_contract=contract,
        )


# ---------- file_back_author defensive seam ----------


@pytest.fixture
def orch_with_magic_memory(monkeypatch: pytest.MonkeyPatch):
    """Stub orchestrator with a MagicMock memory service.

    The defensive seam only inspects the plan type; if it's a
    ``_SectionRefusal`` the receipt is returned without touching the
    memory service. We stub ``apply_plan`` so we can confirm that
    branch is skipped.
    """
    wiki = MagicMock()
    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
    orch.wiki = wiki
    orch._memory_service = MagicMock()
    return orch


def test_file_back_author_refuses_when_plan_carries_refusal(
    orch_with_magic_memory: Orchestrator, contract: SectionContract
) -> None:
    """A refusal passed into ``file_back_author`` short-circuits to a receipt.

    This is the defensive belt-and-braces seam: even if a
    ``_SectionRefusal`` reaches ``file_back_author`` from somewhere
    other than ``build_author_plan`` (e.g. a future caller), the
    orchestrator must surface it as an errors-as-value receipt
    rather than crashing the apply-with-retry loop.
    """
    refusal = _SectionRefusal(
        error="missing required section(s) for synthesis: ## Evidence, ## Open Questions",
        page_type="synthesis",
        slug="what-is-x",
        title="What is X",
    )
    receipt = orch_with_magic_memory.file_back_author(refusal)
    assert receipt.changed_pages == []
    assert receipt.errors
    # Error string names the missing sections so the operator sees the cause.
    joined = " ".join(receipt.errors)
    assert "## Evidence" in joined
    assert "## Open Questions" in joined
    # The memory service is never called on the refusal branch.
    orch_with_magic_memory._memory_service.apply_plan.assert_not_called()


def test_file_back_author_normal_path_unchanged(
    orch_with_magic_memory: Orchestrator,
) -> None:
    """Sanity: a real ``MemoryPlan`` still flows through apply_plan."""
    from lies.memory.models import MemoryReceipt

    orch_with_magic_memory._memory_service.register_evidence = MagicMock()
    orch_with_magic_memory._memory_service.apply_plan = MagicMock(
        return_value=MemoryReceipt(
            changed_pages=[],
            deferred=[],
            fallback_used=False,
            fallback_reason="",
            errors=[],
        )
    )
    plan = MemoryPlan(
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
    receipt = orch_with_magic_memory.file_back_author(plan)
    orch_with_magic_memory._memory_service.apply_plan.assert_called_once()
    assert receipt.errors == []
