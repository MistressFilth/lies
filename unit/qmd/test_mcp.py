"""Unit tests for ``lies.qmd.mcp.QmdRecycleToolset``.

Wraps an inner MCPToolset and recycles the qmd daemon on transport-class
failures. Three failure modes:

- ``httpx.ReadTimeout`` (wedge): recycle + raise ``ModelRetry``; no inner retry.
- ``httpx.TransportError`` (daemon down / starting): recycle + retry once.
- ``mcp.MCPError(code=REQUEST_TIMEOUT)`` (fastmcp wraps
  ``httpx.ConnectTimeout``): recycle + retry once.

A recycle itself failing (``QmdRecycleFailed``) surfaces as
``ToolFailed("qmd daemon recycled but never served")``.

Other ``mcp.MCPError`` codes (e.g. ``-32602`` Invalid params) pass through
unchanged — those are protocol-level rejections, not transport failures.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import mcp
import pytest
from pydantic_ai import ModelRetry, ToolFailed
from pydantic_ai._run_context import RunContext

from lies.qmd.daemon import QmdRecycleFailed, QmdState
from lies.qmd.mcp import QmdRecycleToolset


class _FakeToolset:
    """Implements the minimum MCPToolset surface QmdRecycleToolset needs."""

    def __init__(self, call_tool: Callable[[], Awaitable[Any]]) -> None:
        self._call_tool = call_tool
        self.call_count = 0

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: Any,
    ) -> Any:
        self.call_count += 1
        return await self._call_tool()


def _dummy_ctx() -> RunContext[Any]:
    # pydantic-ai 1.x made RunContext kwarg-only and added many fields;
    # the wrapper only consumes ctx as a pass-through, so a minimally
    # populated object is sufficient for these behavioral assertions.
    return RunContext(  # type: ignore[call-arg]
        deps=None,  # type: ignore[arg-type]
        model=None,  # type: ignore[arg-type]
        usage=None,  # type: ignore[arg-type]
        prompt=None,  # type: ignore[arg-type]
    )


def _dummy_tool() -> Any:
    # ``tool`` is passed through to the inner ``call_tool`` unchanged and the
    # wrapper never inspects it. A bare MagicMock stands in.
    return MagicMock()


@pytest.mark.asyncio
async def test_qmd_recycle_toolset_passes_through_on_success() -> None:
    """Wrapper returns success without invoking recycle_cb."""
    inner = _FakeToolset(call_tool=AsyncMock(return_value={"ok": True}))
    recycle_cb = AsyncMock(return_value=QmdState(True, True, 1, "running"))
    wrapper = QmdRecycleToolset(wrapped=inner, recycle_cb=recycle_cb)

    result = await wrapper.call_tool("qmd_query", {}, _dummy_ctx(), _dummy_tool())

    assert result == {"ok": True}
    assert inner.call_count == 1
    recycle_cb.assert_not_called()


@pytest.mark.asyncio
async def test_qmd_recycle_toolset_read_timeout_recycles_and_raises_model_retry() -> None:
    """ReadTimeout → recycle + ModelRetry (no inner retry on wedge)."""

    def _make_inner() -> _FakeToolset:
        async def call_tool() -> None:
            raise httpx.ReadTimeout("wedged")

        return _FakeToolset(call_tool=call_tool)

    inner = _make_inner()
    recycle_cb = AsyncMock(return_value=QmdState(True, True, 7, "fresh"))
    wrapper = QmdRecycleToolset(wrapped=inner, recycle_cb=recycle_cb)

    with pytest.raises(ModelRetry) as excinfo:
        await wrapper.call_tool("qmd_query", {}, _dummy_ctx(), _dummy_tool())

    assert "recycled" in str(excinfo.value)
    assert "pid 7" in str(excinfo.value)
    recycle_cb.assert_awaited_once()
    assert inner.call_count == 1  # no inner retry on wedge


@pytest.mark.asyncio
async def test_qmd_recycle_toolset_transport_error_recycles_and_retries_once() -> None:
    """TransportError → recycle + retry once → return success."""
    attempt = {"n": 0}

    async def call_tool() -> Any:
        attempt["n"] += 1
        if attempt["n"] == 1:
            raise httpx.TransportError("connect refused")
        return {"ok": "after-retry"}

    inner = _FakeToolset(call_tool=call_tool)
    recycle_cb = AsyncMock(return_value=QmdState(True, True, 8, "fresh"))
    wrapper = QmdRecycleToolset(wrapped=inner, recycle_cb=recycle_cb)

    result = await wrapper.call_tool("qmd_query", {}, _dummy_ctx(), _dummy_tool())

    assert result == {"ok": "after-retry"}
    assert attempt["n"] == 2  # one failure + one success
    recycle_cb.assert_awaited_once()


@pytest.mark.asyncio
async def test_qmd_recycle_toolset_transport_error_retry_also_fails_raises_tool_failed() -> None:
    """TransportError → recycle + retry → still fails → ToolFailed."""

    async def call_tool() -> None:
        raise httpx.TransportError("still down")

    inner = _FakeToolset(call_tool=call_tool)
    recycle_cb = AsyncMock(return_value=QmdState(True, True, 9, "fresh"))
    wrapper = QmdRecycleToolset(wrapped=inner, recycle_cb=recycle_cb)

    with pytest.raises(ToolFailed) as excinfo:
        await wrapper.call_tool("qmd_query", {}, _dummy_ctx(), _dummy_tool())

    assert "still unreachable after recycle" in str(excinfo.value)
    recycle_cb.assert_awaited_once()
    assert inner.call_count == 2  # tried twice


@pytest.mark.asyncio
async def test_qmd_recycle_toolset_mcp_error_timeout_recycles_and_retries_once() -> None:
    """MCPError(REQUEST_TIMEOUT) → recycle + retry once → return success."""
    attempt = {"n": 0}

    async def call_tool() -> Any:
        attempt["n"] += 1
        if attempt["n"] == 1:
            raise mcp.MCPError(
                code=httpx.codes.REQUEST_TIMEOUT,
                message="Timed out while waiting for response.",
            )
        return {"ok": "after-retry"}

    inner = _FakeToolset(call_tool=call_tool)
    recycle_cb = AsyncMock(return_value=QmdState(True, True, 10, "fresh"))
    wrapper = QmdRecycleToolset(wrapped=inner, recycle_cb=recycle_cb)

    result = await wrapper.call_tool("qmd_query", {}, _dummy_ctx(), _dummy_tool())

    assert result == {"ok": "after-retry"}
    recycle_cb.assert_awaited_once()


@pytest.mark.asyncio
async def test_qmd_recycle_toolset_other_mcp_error_passes_through() -> None:
    """Non-timeout MCPError re-raises unchanged; recycle_cb not invoked."""
    err = mcp.MCPError(code=-32602, message="Invalid params")

    async def call_tool() -> None:
        raise err

    inner = _FakeToolset(call_tool=call_tool)
    recycle_cb = AsyncMock(return_value=QmdState(True, True, 11, "fresh"))
    wrapper = QmdRecycleToolset(wrapped=inner, recycle_cb=recycle_cb)

    with pytest.raises(mcp.MCPError) as excinfo:
        await wrapper.call_tool("qmd_query", {}, _dummy_ctx(), _dummy_tool())

    assert excinfo.value is err
    recycle_cb.assert_not_called()


@pytest.mark.asyncio
async def test_qmd_recycle_toolset_recycle_failure_raises_tool_failed() -> None:
    """When recycle itself raises QmdRecycleFailed, wrapper raises ToolFailed."""

    async def call_tool() -> None:
        raise httpx.ReadTimeout("wedged")

    inner = _FakeToolset(call_tool=call_tool)
    recycle_cb = AsyncMock(
        side_effect=QmdRecycleFailed(30.0, QmdState(False, False, None, "stuck"))
    )
    wrapper = QmdRecycleToolset(wrapped=inner, recycle_cb=recycle_cb)

    with pytest.raises(ToolFailed) as excinfo:
        await wrapper.call_tool("qmd_query", {}, _dummy_ctx(), _dummy_tool())

    assert "recycled but never served" in str(excinfo.value)
