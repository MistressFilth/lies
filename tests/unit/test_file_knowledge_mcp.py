"""Tests for MCP file_knowledge tool — collision + force gate, no elicit yet.

Elicit branch (overwrite/rename/cancel) lands in Task 12.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lies.memory.models import MemoryReceipt


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
