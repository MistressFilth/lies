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

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lies.memory.models import MemoryPlan, MemoryReceipt, PageCreate
from lies.orchestrator import Orchestrator
from lies.page import build_author_plan
from lies.page.author import _SectionRefusal
from lies.query.citation import Citation
from lies.query.models import SynthesizedAnswer
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


@pytest.mark.parametrize(
    ("page_type", "body", "section_contract"),
    [
        # No section_contract (default-None): existing F39 callers that
        # don't pass ``section_contract`` see no behaviour change —
        # the refusal seam is disabled.
        ("entity", "## Overview\n\nx\n", None),
        # Empty contract: the per-wiki schema has no required
        # headings, so any body passes.
        ("entity", "anything goes", SectionContract()),
    ],
    ids=["no_contract_default", "empty_contract"],
)
def test_build_author_plan_no_enforcement_when_contract_allows(
    page_type: str, body: str, section_contract: SectionContract | None
) -> None:
    """When the contract permits the body — either ``None`` (no
    contract threaded) or an explicitly empty contract — ``build_author_plan``
    returns a ``MemoryPlan`` and never a ``_SectionRefusal``. The two
    ids pin distinct contract-resolution paths (default vs explicit
    empty) that callers depend on.
    """
    plan = build_author_plan(
        type=page_type,
        collection="default",
        slug="widget",
        title="Widget",
        body=body,
        derived_from=[],
        tags=[],
        sources=[],
        exists=_exists_always_false,
        sha_lookup=_sha_lookup,
        section_contract=section_contract,
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
    memory service. ``apply_plan`` is stubbed so the test confirms
    that branch is skipped on refusal.
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
    """Defensive seam: a ``_SectionRefusal`` reaching ``file_back_author``
    (e.g. from a future caller) must surface as an errors-as-value
    ``MemoryReceipt`` rather than crashing the apply-with-retry loop.
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


# ---------- file_back_synthesis end-to-end refusal path ----------


@pytest.fixture
def orch_with_real_section_contract(tmp_path):
    """Stub orchestrator whose ``wiki`` carries a real ``SectionContract``.

    ``orch_with_magic_memory`` uses ``MagicMock()`` for ``wiki``, so
    ``self.wiki.section_contract.__iter__`` yields nothing and the
    refusal seam never fires through ``file_back_synthesis``. This
    fixture swaps in a real ``SectionContract`` so the production
    wiring (threading ``self.wiki.section_contract`` into
    ``build_author_plan``) is exercised end-to-end.
    """
    contract = SectionContract(
        entity=["## Overview", "## Description", "## References"],
        synthesis=["## Thesis", "## Evidence", "## Open Questions"],
    )
    wiki = SimpleNamespace(section_contract=contract)
    wiki.wiki_dir = tmp_path
    with patch("lies.orchestrator.Orchestrator.__init__", lambda self, wiki: None):
        orch = Orchestrator.__new__(Orchestrator)
    orch.wiki = wiki
    orch._memory_service = MagicMock()
    # ``sha_lookup`` inside ``file_back_synthesis`` calls
    # ``self._memory_service.current_state(r)[0]``; return a tuple
    # whose index 0 is a valid sha-like string. The synthesis branch
    # also passes a non-existent slug so ``exists`` is False and the
    # builder takes the PageCreate path (not the PageUpdate path that
    # would also need the sha).
    orch._memory_service.current_state = MagicMock(return_value=("f" * 64, ""))
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


def test_file_back_synthesis_refuses_missing_required_sections(
    orch_with_real_section_contract: Orchestrator,
) -> None:
    """End-to-end: synthesis body missing required sections -> refusal receipt.

    With a real ``SectionContract`` on the wiki (the production wiring
    that the MagicMock-based test could not exercise), a synthesis body
    that lacks the required ``## Thesis`` / ``## Evidence`` /
    ``## Open Questions`` headings must short-circuit to a refusal
    receipt through ``Orchestrator.file_back_synthesis`` — not a
    silently-written page. The refusal seam lives in
    :func:`build_author_plan`; ``file_back_synthesis`` threads
    ``self.wiki.section_contract`` through to the builder, so a real
    contract on the wiki is the only way to reach it from the
    orchestrator.
    """
    answer = SynthesizedAnswer(
        answer="Some prose that does not carry any of the required synthesis headings.",
        citations=[],
        pages_read=[Citation(path="claude-code/concepts/hooks", source="wiki")],
        should_file=True,
        question="What is a hook?",
    )
    receipt = orch_with_real_section_contract.file_back_synthesis(answer, collection="claude-code")
    # Refusal: no write happened, errors list names the missing headings.
    assert receipt.changed_pages == []
    assert receipt.errors
    joined = " ".join(receipt.errors)
    assert "## Thesis" in joined
    assert "## Evidence" in joined
    assert "## Open Questions" in joined
    # The memory service is never called on the refusal branch —
    # ``file_back_synthesis`` delegates to ``file_back_author`` whose
    # defensive refusal seam short-circuits before ``apply_plan``.
    orch_with_real_section_contract._memory_service.apply_plan.assert_not_called()
