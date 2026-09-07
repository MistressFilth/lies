"""Tests for file_knowledge with mocked Context returning elicit verdicts."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lies.memory.models import MemoryReceipt, PageReference
from lies.memory.models import OperationKind


class _Verdict:
    def __init__(self, action, new_slug=None):
        self.action = action
        self.new_slug = new_slug


@pytest.fixture
def ctx_overwrite():
    return MagicMock(elicit=MagicMock(return_value=_Verdict("overwrite")))


@pytest.fixture
def ctx_rename():
    return MagicMock(elicit=MagicMock(return_value=_Verdict("rename", new_slug="hooks-v2")))


@pytest.fixture
def ctx_cancel():
    return MagicMock(elicit=MagicMock(return_value=_Verdict("cancel")))


def _setup():
    wiki = MagicMock()
    wiki.wiki_dir = MagicMock()
    wiki.wiki_dir.__truediv__.return_value.exists.return_value = True  # collision
    orch = MagicMock()
    orch.file_back_author = MagicMock(
        return_value=MemoryReceipt(
            changed_pages=[
                PageReference(
                    path="claude-code/concepts/hooks.md",
                    collection_id="claude-code",
                    op=OperationKind.UPDATE,
                )
            ],
            deferred=[],
            fallback_used=False,
            fallback_reason="",
            errors=[],
        )
    )
    orch._memory_service.current_state = MagicMock(return_value=("f" * 64,))
    return wiki, orch


def test_elicit_overwrite_proceeds_with_existing_path(ctx_overwrite):
    from unittest.mock import patch

    from lies.mcp.server import file_knowledge

    wiki, orch = _setup()
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=wiki),
        patch("lies.mcp.server.Orchestrator", return_value=orch),
    ):
        result = file_knowledge(
            page_type="concept",
            collection="claude-code",
            slug="hooks",
            title="Hooks",
            body="body",
            ctx=ctx_overwrite,
        )
    assert result["op"] == "update"
    assert orch.file_back_author.call_count == 1


def test_elicit_rename_uses_new_slug(ctx_rename):
    from unittest.mock import patch

    from lies.mcp.server import file_knowledge

    wiki, orch = _setup()
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=wiki),
        patch("lies.mcp.server.Orchestrator", return_value=orch),
    ):
        result = file_knowledge(
            page_type="concept",
            collection="claude-code",
            slug="hooks",
            title="Hooks",
            body="body",
            ctx=ctx_rename,
        )
    assert result["slug"] == "hooks-v2"
    assert "hooks-v2.md" in result["page_path"]


def test_elicit_cancel_returns_cancelled_receipt(ctx_cancel):
    from unittest.mock import patch

    from lies.mcp.server import file_knowledge

    wiki, orch = _setup()
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=wiki),
        patch("lies.mcp.server.Orchestrator", return_value=orch),
    ):
        result = file_knowledge(
            page_type="concept",
            collection="claude-code",
            slug="hooks",
            title="Hooks",
            body="body",
            ctx=ctx_cancel,
        )
    assert result["op"] == "none"
    assert result["receipt"]["errors"] == ["cancelled by operator"]
    assert orch.file_back_author.call_count == 0


def test_elicit_rename_without_new_slug_raises_tool_error():
    from unittest.mock import patch

    from fastmcp.exceptions import ToolError

    from lies.mcp.server import file_knowledge

    wiki, orch = _setup()
    bad_ctx = MagicMock(elicit=MagicMock(return_value=_Verdict("rename", new_slug=None)))
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=wiki),
        patch("lies.mcp.server.Orchestrator", return_value=orch),
    ):
        with pytest.raises(ToolError, match="rename requires new_slug"):
            file_knowledge(
                page_type="concept",
                collection="c",
                slug="s",
                title="T",
                body="b",
                ctx=bad_ctx,
            )
