"""Tests for file_knowledge with mocked Context returning elicit verdicts.

FastMCP's ``ctx.elicit`` returns one of three wrapper types whose ``.action``
distinguishes the outcome:

- ``AcceptedElicitation[T]`` — ``.action == "accept"``; the user's choice
  lives at ``.data`` (here a ``_CollisionVerdict`` with ``.action`` and
  ``.new_slug``).
- ``DeclinedElicitation`` — ``.action == "decline"``.
- ``CancelledElicitation`` — ``.action == "cancel"``.

These tests use ``AsyncMock`` (because ``ctx.elicit`` is a coroutine) and
construct real ``AcceptedElicitation`` instances via FastMCP's exported
wrapper so the contract under test matches what production will see.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.server.elicitation import AcceptedElicitation

from lies.memory.models import MemoryReceipt, PageReference
from lies.memory.models import OperationKind


# FastMCP does not re-export ``DeclinedElicitation`` / ``CancelledElicitation``
# (only ``AcceptedElicitation``), so we import them from the underlying
# ``mcp.server.elicitation`` module which FastMCP's wrappers delegate to.
from mcp.server.elicitation import (  # noqa: E402
    CancelledElicitation,
    DeclinedElicitation,
)


@pytest.fixture
def ctx_overwrite():
    """Mock Context where the user accepts and chooses 'overwrite'."""
    return MagicMock(
        elicit=AsyncMock(
            return_value=AcceptedElicitation(
                data=_CollisionVerdict(action="overwrite", new_slug=None),
            ),
        ),
    )


@pytest.fixture
def ctx_rename():
    """Mock Context where the user accepts and chooses 'rename' with a slug."""
    return MagicMock(
        elicit=AsyncMock(
            return_value=AcceptedElicitation(
                data=_CollisionVerdict(action="rename", new_slug="hooks-v2"),
            ),
        ),
    )


@pytest.fixture
def ctx_cancel():
    """Mock Context where the user cancels the elicitation at the wrapper level."""
    return MagicMock(
        elicit=AsyncMock(return_value=CancelledElicitation()),
    )


@pytest.fixture
def ctx_decline():
    """Mock Context where the user declines the elicitation at the wrapper level."""
    return MagicMock(
        elicit=AsyncMock(return_value=DeclinedElicitation()),
    )


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


class _CollisionVerdict:
    """Verdict the user submits inside an ``AcceptedElicitation.data``.

    Mirrors ``lies.mcp.server._CollisionVerdict`` but is duck-typed here so
    the test does not depend on the server module's import order. The two
    literals must stay in sync (``Literal["overwrite", "rename", "cancel"]``).
    """

    __slots__ = ("action", "new_slug")

    def __init__(self, action: str, new_slug: str | None = None) -> None:
        self.action = action
        self.new_slug = new_slug


async def test_elicit_overwrite_proceeds_with_existing_path(ctx_overwrite):
    from unittest.mock import patch

    from lies.mcp.server import file_knowledge

    wiki, orch = _setup()
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=wiki),
        patch("lies.mcp.server.Orchestrator", return_value=orch),
    ):
        result = await file_knowledge(
            page_type="concept",
            collection="claude-code",
            slug="hooks",
            title="Hooks",
            body="body",
            ctx=ctx_overwrite,
        )
    assert result["op"] == "update"
    assert orch.file_back_author.call_count == 1


async def test_elicit_rename_uses_new_slug(ctx_rename):
    from unittest.mock import patch

    from lies.mcp.server import file_knowledge

    wiki, orch = _setup()
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=wiki),
        patch("lies.mcp.server.Orchestrator", return_value=orch),
    ):
        result = await file_knowledge(
            page_type="concept",
            collection="claude-code",
            slug="hooks",
            title="Hooks",
            body="body",
            ctx=ctx_rename,
        )
    assert result["slug"] == "hooks-v2"
    assert "hooks-v2.md" in result["page_path"]


async def test_elicit_cancel_returns_cancelled_receipt(ctx_cancel):
    from unittest.mock import patch

    from lies.mcp.server import file_knowledge

    wiki, orch = _setup()
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=wiki),
        patch("lies.mcp.server.Orchestrator", return_value=orch),
    ):
        result = await file_knowledge(
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


async def test_elicit_decline_returns_cancelled_receipt(ctx_decline):
    """User clicking 'decline' must NOT silently overwrite; treated as cancel."""
    from unittest.mock import patch

    from lies.mcp.server import file_knowledge

    wiki, orch = _setup()
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=wiki),
        patch("lies.mcp.server.Orchestrator", return_value=orch),
    ):
        result = await file_knowledge(
            page_type="concept",
            collection="claude-code",
            slug="hooks",
            title="Hooks",
            body="body",
            ctx=ctx_decline,
        )
    assert result["op"] == "none"
    assert result["receipt"]["errors"] == ["cancelled by operator"]
    assert orch.file_back_author.call_count == 0


async def test_elicit_rename_without_new_slug_raises_tool_error():
    from unittest.mock import patch

    from fastmcp.exceptions import ToolError

    from lies.mcp.server import file_knowledge

    wiki, orch = _setup()
    bad_ctx = MagicMock(
        elicit=AsyncMock(
            return_value=AcceptedElicitation(
                data=_CollisionVerdict(action="rename", new_slug=None),
            ),
        ),
    )
    with (
        patch("lies.mcp.server.resolve_wiki", return_value=wiki),
        patch("lies.mcp.server.Orchestrator", return_value=orch),
    ):
        with pytest.raises(ToolError, match="rename requires new_slug"):
            await file_knowledge(
                page_type="concept",
                collection="c",
                slug="s",
                title="T",
                body="b",
                ctx=bad_ctx,
            )
