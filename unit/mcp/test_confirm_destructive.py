"""Tests for _confirm_destructive MCP helper (ask-mirrored)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.server.elicitation import AcceptedElicitation
from mcp.server.elicitation import (
    CancelledElicitation,
    DeclinedElicitation,
)

from lies.mcp.server import _ConfirmDestructive, _confirm_destructive


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
def ctx_decline_data():
    """accept but confirm=False."""
    return MagicMock(
        elicit=AsyncMock(
            return_value=AcceptedElicitation(
                data=_ConfirmDestructive(confirm=False, reason="no"),
            ),
        ),
    )


@pytest.fixture
def ctx_cancel():
    return MagicMock(
        elicit=AsyncMock(
            return_value=CancelledElicitation(),
        ),
    )


@pytest.fixture
def ctx_decline():
    return MagicMock(
        elicit=AsyncMock(
            return_value=DeclinedElicitation(),
        ),
    )


@pytest.fixture
def ctx_raises():
    return MagicMock(
        elicit=AsyncMock(side_effect=RuntimeError("no elicit support")),
    )


@pytest.mark.asyncio
async def test_accept_returns_none(ctx_accept) -> None:
    assert await _confirm_destructive(ctx_accept, "msg") is None


@pytest.mark.asyncio
async def test_accept_with_confirm_false_returns_decline(ctx_decline_data) -> None:
    assert await _confirm_destructive(ctx_decline_data, "msg") == "operation declined by user"


@pytest.mark.asyncio
async def test_cancel_returns_decline(ctx_cancel) -> None:
    assert await _confirm_destructive(ctx_cancel, "msg") == "operation declined by user"


@pytest.mark.asyncio
async def test_decline_returns_decline(ctx_decline) -> None:
    assert await _confirm_destructive(ctx_decline, "msg") == "operation declined by user"


@pytest.mark.asyncio
async def test_elicitation_unavailable_returns_error(ctx_raises) -> None:
    result = await _confirm_destructive(ctx_raises, "msg")
    assert result is not None
    assert "elicitation unavailable" in result
