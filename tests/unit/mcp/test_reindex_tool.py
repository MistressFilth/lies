"""Tests for the MCP reindex tool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.server.elicitation import AcceptedElicitation

from lies.mcp.server import _ConfirmDestructive, mcp, reindex as reindex_tool
from lies.qmd import _models, cli as qmd_cli


@pytest.fixture
def ctx_accept():
    return MagicMock(
        elicit=AsyncMock(
            return_value=AcceptedElicitation(
                data=_ConfirmDestructive(confirm=True, reason=""),
            ),
        ),
    )


@pytest.fixture
def mock_qmd_reindex():
    with patch.object(
        qmd_cli,
        "qmd_reindex",
        return_value=_models.ReindexResult(indexed=True, cleaned=True),
    ) as mock:
        yield mock


@pytest.fixture
def mock_resolve_wiki():
    wiki = MagicMock()
    wiki.wiki_dir = Path("/tmp/wiki")
    with patch("lies.mcp.server.resolve_wiki", return_value=wiki):
        yield wiki


@pytest.mark.asyncio
async def test_reindex_no_flags_calls_qmd_reindex(
    ctx_accept, mock_qmd_reindex, mock_resolve_wiki
) -> None:
    """No flags: qmd_reindex called once, no elicit."""
    await reindex_tool(ctx=ctx_accept, name="t")
    mock_qmd_reindex.assert_called_once()
    ctx_accept.elicit.assert_not_called()


@pytest.mark.asyncio
async def test_reindex_force_no_elicitation(
    ctx_accept, mock_qmd_reindex, mock_resolve_wiki
) -> None:
    """--force alone (non-destructive) doesn't elicit."""
    result = await reindex_tool(force=True, ctx=ctx_accept, name="t")
    ctx_accept.elicit.assert_not_called()
    assert result["indexed"] is True


@pytest.mark.asyncio
async def test_reindex_embed_no_elicitation(
    ctx_accept, mock_qmd_reindex, mock_resolve_wiki
) -> None:
    """--embed alone (non-destructive) doesn't elicit."""
    await reindex_tool(embed=True, ctx=ctx_accept, name="t")
    ctx_accept.elicit.assert_not_called()


@pytest.mark.asyncio
async def test_reindex_cleanup_accepted_calls_qmd(
    ctx_accept, mock_qmd_reindex, mock_resolve_wiki
) -> None:
    """cleanup=True + accept: qmd called, cleaned=True."""
    result = await reindex_tool(cleanup=True, ctx=ctx_accept, name="t")
    ctx_accept.elicit.assert_awaited_once()
    mock_qmd_reindex.assert_called_once()
    assert result["cleaned"] is True


@pytest.mark.asyncio
async def test_reindex_cleanup_declined_no_qmd(mock_resolve_wiki) -> None:
    """cleanup=True + decline: qmd NOT called; returns decline error."""
    from mcp.server.elicitation import CancelledElicitation

    ctx = MagicMock(
        elicit=AsyncMock(return_value=CancelledElicitation()),
    )
    with patch.object(qmd_cli, "qmd_reindex") as mock:
        result = await reindex_tool(cleanup=True, ctx=ctx, name="t")
    mock.assert_not_called()
    assert result["errors"] == ["operation declined by user"]
    assert result["cleaned"] is False


@pytest.mark.asyncio
async def test_reindex_elicitation_unavailable_no_qmd(mock_resolve_wiki) -> None:
    """ctx.elicit raises: treated as decline; qmd NOT called."""
    ctx = MagicMock(
        elicit=AsyncMock(side_effect=RuntimeError("no support")),
    )
    with patch.object(qmd_cli, "qmd_reindex") as mock:
        result = await reindex_tool(cleanup=True, ctx=ctx, name="t")
    mock.assert_not_called()
    assert len(result["errors"]) == 1
    assert "elicitation unavailable" in result["errors"][0]


@pytest.mark.asyncio
async def test_reindex_all_elicit_with_all_text(
    ctx_accept, mock_qmd_reindex, mock_resolve_wiki
) -> None:
    """--all elicits with 'all+cleanup' prompt text."""
    await reindex_tool(all_=True, ctx=ctx_accept, name="t")
    elicit_args = ctx_accept.elicit.call_args
    msg = elicit_args[0][0]  # first positional arg
    assert "all" in msg.lower()


@pytest.mark.asyncio
async def test_reindex_cleanup_no_ctx_no_work(mock_resolve_wiki, mock_qmd_reindex) -> None:
    """ctx=None + cleanup=True: gate runs; helper raises on ctx.elicit;
    qmd_reindex NOT called; result carries 'elicitation unavailable'."""
    result = await reindex_tool(cleanup=True, ctx=None, name="t")
    mock_qmd_reindex.assert_not_called()
    assert result["indexed"] is False
    assert len(result["errors"]) == 1
    assert "elicitation unavailable" in result["errors"][0]


@pytest.mark.asyncio
async def test_reindex_destructive_hint_annotation() -> None:
    """The registered reindex tool advertises destructiveHint=True so
    host UIs can flag the surface even before the user passes the
    destructive flags."""
    tool = await mcp.get_tool("reindex")
    assert tool.annotations is not None
    # Access via the snake_case source name — the same name we passed
    # at registration — to sidestep alias-resolution differences across
    # mcp SDK versions.
    assert tool.annotations.destructive_hint is True
