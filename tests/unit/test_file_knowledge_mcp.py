"""Tests for MCP file_knowledge tool — collision + force gate, no elicit yet.

Elicit branch (overwrite/rename/cancel) lands in Task 12.

F17 (Task 4) tests: ``file_knowledge`` must surface a
``_SectionRefusal`` from ``build_author_plan`` as a refusal-shaped
``WriteKnowledgeResult`` (``op="none"``, ``page_path=None``,
``receipt["errors"]`` names the missing headings). The MCP layer is
the canonical refusal seam for LLM callers; it short-circuits before
``Orchestrator.file_back_author`` runs.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lies.memory.models import MemoryReceipt
from lies.page.author import _SectionRefusal


@pytest.fixture
def mock_servicer():
    """Patch WikiMemoryService + Orchestrator inside server module."""
    with (
        patch("lies.mcp.server.resolve_wiki"),
        patch("lies.mcp.server.Orchestrator"),
    ):
        wiki = MagicMock()
        wiki.wiki_dir = MagicMock()
        orch = MagicMock()
        orch.file_back_author = MagicMock(
            return_value=MemoryReceipt(
                changed_pages=[],
                deferred=[],
                fallback_used=False,
                fallback_reason="",
                errors=[],
            )
        )
        orch._memory_service.current_state = MagicMock(return_value=("f" * 64,))
        yield {"wiki": wiki, "orch": orch}


async def test_file_knowledge_round_trip(mock_servicer):
    from lies.mcp.server import file_knowledge

    mock_servicer["wiki"].wiki_dir.__truediv__.return_value.exists.return_value = False
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=mock_servicer["wiki"]),
        patch("lies.mcp.server.Orchestrator", return_value=mock_servicer["orch"]),
    ):
        result = await file_knowledge(
            page_type="concept",
            collection="claude-code",
            slug="hooks",
            title="Hooks",
            body="body",
        )
    assert result["page_type"] == "concept"
    assert result["op"] == "create"


async def test_file_knowledge_collision_no_force_no_ctx_raises_tool_error(mock_servicer):
    from fastmcp.exceptions import ToolError

    from lies.mcp.server import file_knowledge

    mock_servicer["wiki"].wiki_dir.__truediv__.return_value.exists.return_value = True
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=mock_servicer["wiki"]),
        patch("lies.mcp.server.Orchestrator", return_value=mock_servicer["orch"]),
    ):
        with pytest.raises(ToolError, match="pass force=True"):
            await file_knowledge(
                page_type="concept",
                collection="c",
                slug="s",
                title="T",
                body="b",
            )


async def test_file_knowledge_plan_invalid_raises_tool_error(mock_servicer):
    from fastmcp.exceptions import ToolError

    from lies.mcp.server import file_knowledge

    mock_servicer["wiki"].wiki_dir.__truediv__.return_value.exists.return_value = False
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=mock_servicer["wiki"]),
        patch("lies.mcp.server.Orchestrator", return_value=mock_servicer["orch"]),
    ):
        with pytest.raises(ToolError, match="plan_invalid"):
            await file_knowledge(
                page_type="concept",
                collection="c",
                slug="s",
                title="T",
                body="   ",  # empty → WikiPlanInvalid
            )


async def test_file_knowledge_refuses_missing_sections(mock_servicer, monkeypatch):
    """F17 Task 4: refusal surface.

    When ``build_author_plan`` returns a ``_SectionRefusal`` (because
    the body omits headings required by the wiki's section contract),
    ``file_knowledge`` must surface that as a refusal-shaped
    ``WriteKnowledgeResult`` rather than crashing or flowing the
    refusal into ``Orchestrator.file_back_author`` and silently
    treating it as an empty success.

    Expected shape (mirrors the cancel/decline elicit branches):
    ``op="none"``, ``page_path=None``, and the missing-heading
    message preserved verbatim in ``receipt["errors"]``.
    """
    from lies.mcp.server import file_knowledge

    refusal = _SectionRefusal(
        error=("missing required section(s) for synthesis: ## Evidence, ## Open Questions"),
        page_type="synthesis",
        slug="what-is-x",
        title="What is X",
    )
    monkeypatch.setattr("lies.mcp.server.build_author_plan", lambda **_: refusal)
    mock_servicer["wiki"].wiki_dir.__truediv__.return_value.exists.return_value = False

    with (
        patch("lies.mcp.server.resolve_wiki", return_value=mock_servicer["wiki"]),
        patch("lies.mcp.server.Orchestrator", return_value=mock_servicer["orch"]),
    ):
        result = await file_knowledge(
            page_type="synthesis",
            collection="default",
            slug="what-is-x",
            title="What is X",
            body="## Thesis\n\nx\n",  # missing Evidence + Open Questions
            ctx=None,
        )

    # Refusal shape: nothing was written.
    assert result["page_path"] is None
    assert result["op"] == "none"
    assert result["page_type"] == "synthesis"
    assert result["slug"] == "what-is-x"
    # Error message preserved verbatim in the receipt envelope.
    assert "## Evidence" in result["receipt"]["errors"][0]
    assert "## Open Questions" in result["receipt"]["errors"][0]
    assert "synthesis" in result["receipt"]["errors"][0]
    # Defensive seam in Orchestrator.file_back_author must NOT have
    # fired: the MCP layer short-circuits before touching the
    # memory service.
    assert mock_servicer["orch"].file_back_author.call_count == 0


async def test_file_knowledge_threads_section_contract_to_plan_builder(mock_servicer, monkeypatch):
    """F17 Task 4: the wiki's resolved contract must reach the plan builder.

    ``Wiki.section_contract`` is the single source of truth for the
    per-wiki required-heading contract (Task 2). The MCP server must
    thread it into ``build_author_plan`` so production wikis see
    enforcement; only the default (``None``) was enforced at Task 3.
    The exact contract object is forwarded; we assert on identity
    rather than equality because ``SectionContract`` is a frozen
    Pydantic model and equality is structural.
    """
    from lies.schema.sections import SectionContract

    from lies.mcp.server import file_knowledge

    captured: dict[str, object] = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        # Return a plan that satisfies everything so the call proceeds.
        return _stub_plan()

    contract = SectionContract(
        synthesis=["## Thesis", "## Evidence", "## Open Questions"],
    )
    mock_servicer["wiki"].section_contract = contract
    mock_servicer["wiki"].wiki_dir.__truediv__.return_value.exists.return_value = False
    monkeypatch.setattr("lies.mcp.server.build_author_plan", _capture)

    with (
        patch("lies.mcp.server.resolve_wiki", return_value=mock_servicer["wiki"]),
        patch("lies.mcp.server.Orchestrator", return_value=mock_servicer["orch"]),
    ):
        await file_knowledge(
            page_type="synthesis",
            collection="default",
            slug="what-is-x",
            title="What is X",
            body=("## Thesis\n\nx\n\n## Evidence\n\n[[e]]\n\n## Open Questions\n\nx\n"),
            ctx=None,
        )

    assert captured["section_contract"] is contract


def _stub_plan():
    """Return a minimal ``MemoryPlan`` shaped like a single-page create."""
    from lies.memory.models import (
        EvidenceAppend,
        MemoryPlan,
        PageCreate,
        PageDelete,
        PageUpdate,
    )

    op: PageCreate | PageUpdate | EvidenceAppend | PageDelete = PageCreate(
        path="default/synthesis/what-is-x.md",
        content="stub",
        evidence=["default/what-is-x"],
        tag="synthesis",
    )
    return MemoryPlan(operations=[op], rationale="stub", evidence=["default/what-is-x"])
