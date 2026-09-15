"""qmd MCP client.

The qmd MCP server exposes `query`, `get`, `multi_get`, `status` tools
that the LIES orchestrator uses for hybrid search.

Usage:
    from pydantic_ai import Agent
    from lies.qmd.mcp import QmdMcpClient

    qmd = QmdMcpClient(transport="stdio")
    agent = Agent("anthropic:claude-opus-4-7", capabilities=[qmd.as_capability()])

Note: the stdio transport needs pydantic-ai's `mcp` extra installed at
runtime — `pip install "pydantic-ai-slim[mcp]"`. Without it the capability
constructs fine but tool calls will fail with an ImportError explaining
the missing dependency.

For stdio the qmd binary is launched as `qmd mcp` through a FastMCP
`StdioTransport`, which is the invocation required for qmd's MCP server.

See https://github.com/tobi/qmd#mcp for the qmd MCP surface.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

import fastmcp
import mcp  # type: ignore[import-not-found]
from fastmcp.client.transports import StreamableHttpTransport
from pydantic_ai import ModelRetry, ToolFailed
from pydantic_ai.toolsets import WrapperToolset

try:
    from pydantic_ai.mcp import MCPToolset  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — pydantic_ai[mcp] is a runtime extra
    MCPToolset: Any = None

if TYPE_CHECKING:
    from lies.qmd.daemon import QmdState  # noqa: F401

_log = logging.getLogger(__name__)


_DEFAULT_HTTPX_TIMEOUTS = httpx.Timeout(connect=2.0, read=60.0, write=10.0, pool=5.0)


def _build_qmd_httpx_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Custom httpx factory with explicit connect/read/write timeouts.

    The qmd daemon's first HyDE query after fresh start can wedge the
    call handler for ~60 s (qexpander cold-start latency, not event-loop
    deadlock; see spec §"Problem"). The default fastmcp http transport
    uses no explicit timeouts, so a wedged daemon surfaces as
    httpx.ReadTimeout only after whatever httpx considers "infinite."
    Setting read=60s bounds the wedge to a single recycle round-trip.
    """
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or _DEFAULT_HTTPX_TIMEOUTS,
        auth=auth,
    )


def _build_qmd_http_toolset(url: str) -> Any:
    """Inner HTTP toolset; custom httpx client; no recycle logic.

    Consumed by Task 5's ``QmdRecycleToolset`` wrapper. Separated here
    so the wrapper is testable without a live daemon — the wrapper
    only needs to know about ``call_tool``.
    """
    assert MCPToolset is not None
    return MCPToolset(
        fastmcp.Client(
            StreamableHttpTransport(
                url,
                httpx_client_factory=_build_qmd_httpx_client,  # type: ignore
            )
        )
    )


@dataclass
class QmdMcpClient:
    """Connection config for the qmd MCP server.

    Attributes:
        transport: Either "stdio" (default; spawns `qmd` as an MCP server)
            or "http" (connects to a running `qmd mcp --http` server).
        url: Required when transport is "http". Defaults to
            "http://localhost:8181".
    """

    transport: str = "stdio"
    url: str = "http://localhost:8181"

    def as_capability(self) -> Any:
        """Return a pydantic-ai capability that exposes qmd's MCP tools.

        The returned object can be passed to `Agent(capabilities=[...])`.
        """
        from pydantic_ai.capabilities import MCP

        if self.transport == "stdio":
            # Local stdio server. The capability needs an `MCPToolset`,
            # which lives behind the `mcp` extra. Defer construction to a
            # factory so the capability can be built without the extra
            # installed; the factory is only invoked when the agent actually
            # wires up its toolsets at runtime.
            def _build_qmd_stdio_toolset() -> Any:
                from fastmcp import Client
                from fastmcp.client.transports import (
                    StdioTransport,
                )
                from pydantic_ai.mcp import MCPToolset

                client = Client(StdioTransport(command="qmd", args=["mcp"]))
                return MCPToolset(client)

            return MCP(local=_build_qmd_stdio_toolset)
        if self.transport == "http":
            return MCP(url=self.url, native=True, local=False)
        raise ValueError(f"Unknown transport: {self.transport}")


@dataclass
class QmdRecycleToolset(WrapperToolset[Any]):
    """Wrap an inner MCPToolset; recycle the qmd daemon on transport errors.

    Three failure modes (matches ask's ``_post_query_locked`` reference
    at ask/repo/ask/scripts/ask.py:965-981):

    - ``httpx.ReadTimeout`` (wedge): recycle + raise ``ModelRetry``. Same
      payload would re-wedge the fresh daemon, so no transparent retry.
      Model sees the failed result, decides whether to retry against
      the fresh daemon.
    - ``httpx.TransportError`` (daemon down / starting): recycle + retry
      once. If retry also fails, raise ``ToolFailed`` so the model sees
      a terminal failure with the real reason.
    - ``mcp.MCPError(code=REQUEST_TIMEOUT)`` (fastmcp wraps
      ``httpx.ConnectTimeout``): recycle + retry once. Same shape as
      TransportError.

    Recycle failure (``QmdRecycleFailed`` from ``recycle_qmd_daemon``)
    surfaces as ``ToolFailed("qmd daemon recycled but never served")``.

    Other ``mcp.MCPError`` codes (e.g. ``-32602`` Invalid params) pass
    through unchanged — those are protocol-level rejections, not
    transport failures.

    Consumed by Task 4's ``_build_native_mcp`` (which wraps the inner
    toolset built by ``_build_qmd_http_toolset`` with the wrapper).
    """

    recycle_cb: Callable[[], Awaitable["QmdState"]] | None = None  # type: ignore[name-defined]

    async def call_tool(  # type: ignore[override]
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: Any,
        tool: Any,
    ) -> Any:
        try:
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)
        except httpx.ReadTimeout:
            state = await self._do_recycle()
            raise ModelRetry(
                f"qmd daemon wedged on call to {name!r}; recycled (pid {state.pid or 'unknown'})"
            ) from None
        except httpx.TransportError:
            await self._do_recycle()
            try:
                return await self.wrapped.call_tool(name, tool_args, ctx, tool)
            except (httpx.TransportError, mcp.MCPError) as e:
                raise ToolFailed(f"qmd daemon still unreachable after recycle: {e}") from e
        except mcp.MCPError as e:
            if e.code == httpx.codes.REQUEST_TIMEOUT:
                await self._do_recycle()
                try:
                    return await self.wrapped.call_tool(name, tool_args, ctx, tool)
                except (httpx.TransportError, mcp.MCPError) as e2:
                    raise ToolFailed(f"qmd daemon still timing out after recycle: {e2}") from e2
            raise

    async def _do_recycle(self) -> Any:
        from lies.qmd.daemon import QmdRecycleFailed  # local import to avoid cycle

        if self.recycle_cb is None:
            raise ToolFailed("qmd recycle callback not configured")
        try:
            state = await self.recycle_cb()
        except QmdRecycleFailed as e:
            _log.warning(
                "qmd recycle exhausted ready_timeout=%gs; last_state=%s",
                e.ready_timeout_s,
                e.last_state.detail,
            )
            raise ToolFailed("qmd daemon recycled but never served") from None
        _log.info(
            "qmd daemon recycled (pid=%s); reason=%s",
            state.pid,
            state.detail,
        )
        return state
