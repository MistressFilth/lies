"""qmd MCP client.

Exposes ``query``, ``get``, ``multi_get``, ``status``. For stdio
the qmd binary is launched as ``qmd mcp`` through a FastMCP
``StdioTransport``; the stdio transport needs pydantic-ai's
``mcp`` extra, or tool calls fail with an ``ImportError``.

``QmdRecycleToolset`` is the *agent* face of qmd access. The
library face is ``lies.qmd.access``; this module re-wraps
that classification in pydantic-ai's ``ModelRetry``/``ToolFailed``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import fastmcp
from fastmcp.client.transports import StreamableHttpTransport
from pydantic_ai import ModelRetry, ToolFailed
from pydantic_ai.toolsets import WrapperToolset

from lies.config import DEFAULT_QMD_URL

try:
    from pydantic_ai.mcp import MCPToolset  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — pydantic_ai[mcp] is a runtime extra
    MCPToolset: Any = None

if TYPE_CHECKING:
    from lies.qmd.daemon import QmdState  # noqa: F401

_log = logging.getLogger(__name__)


def _default_read_timeout_s() -> float:
    """The read deadline for a daemon call, in seconds.

    Read at call time from :func:`lies.config.get_qmd_query_timeout`,
    the single source for every qmd retrieval deadline. Connect/
    write/pool stay literal (they bound the handshake).
    """
    from lies.config import get_qmd_query_timeout

    return float(get_qmd_query_timeout())


def _build_qmd_httpx_client(
    headers: dict[str, str] | None = None,
    timeout: Any = None,
    auth: Any = None,
    **kwargs: Any,
) -> Any:
    """Custom httpx factory with explicit connect/read/write timeouts.

    The qmd daemon's first HyDE query after fresh start can wedge
    ~60 s; bounding the read keeps the wedge to a single recycle.

    ``**kwargs`` and ``Any`` are not laziness — fastmcp calls this
    with ``follow_redirects=True``, and a fixed signature made
    every HTTP daemon call fail at connect before the recycle
    taxonomy could run. Built from the httpx module fastmcp
    installed (its vendored ``httpx2`` in 4.0); falls back to
    ``httpx`` if that goes away.
    """
    http_lib = _httpx_fastmcp_installed()
    client = http_lib.AsyncClient(
        headers=headers,
        timeout=timeout
        or http_lib.Timeout(
            connect=2.0,
            read=_default_read_timeout_s(),
            write=10.0,
            pool=5.0,
        ),
        auth=auth,
        **kwargs,
    )
    return client


def _httpx_fastmcp_installed() -> Any:
    """The httpx module fastmcp's own HTTP transport is built against."""
    import importlib

    for name in ("httpx2", "httpx"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    msg = "no httpx module available for the qmd MCP transport"  # pragma: no cover
    raise RuntimeError(msg)  # pragma: no cover


def _build_qmd_http_toolset(url: str) -> Any:
    """Inner HTTP toolset; no recycle logic (separated for testability)."""
    assert MCPToolset is not None
    return MCPToolset(
        fastmcp.Client(
            StreamableHttpTransport(
                url,
                httpx_client_factory=_build_qmd_httpx_client,
            )
        )
    )


@dataclass
class QmdMcpClient:
    """Connection config for the qmd MCP server.

    Attributes:
        transport: ``"stdio"`` (spawns ``qmd``) or ``"http"``.
        url: Defaults to ``config.DEFAULT_QMD_URL``.
    """

    transport: str = "stdio"
    url: str = DEFAULT_QMD_URL

    def as_capability(self) -> Any:
        """Return a pydantic-ai capability exposing qmd's MCP tools."""
        from pydantic_ai.capabilities import MCP

        if self.transport == "stdio":
            # Defer to a factory: the capability needs an
            # ``MCPToolset`` behind the ``mcp`` extra.
            def _build_qmd_stdio_toolset() -> Any:
                from fastmcp import Client
                from fastmcp.client.transports import StdioTransport
                from pydantic_ai.mcp import MCPToolset

                client = Client(StdioTransport(command="qmd", args=["mcp"]))
                return MCPToolset(client)

            return MCP(local=_build_qmd_stdio_toolset)
        if self.transport == "http":
            return MCP(url=self.url, native=True, local=False)
        raise ValueError(f"Unknown transport: {self.transport}")


@dataclass
class QmdRecycleToolset(WrapperToolset[Any]):
    """Wrap an inner ``MCPToolset``; recycle the qmd daemon on transport errors.

    Classification lives in :func:`lies.qmd.access.classify_call_error`;
    this class re-wraps so a model can decide (``ModelRetry``).
    ``ModelRetry`` never escapes into the library path. Three
    modes: **wedge** (recycle + raise ``ModelRetry``),
    **unreachable** (recycle + retry once; ``ToolFailed`` on
    second failure), **not ours** (re-raise unchanged). Recycle
    failure surfaces as
    ``ToolFailed("qmd daemon recycled but never served")``.
    """

    recycle_cb: Callable[[], Awaitable["QmdState"]] | None = None  # type: ignore[name-defined]

    async def call_tool(  # type: ignore[override]
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: Any,
        tool: Any,
    ) -> Any:
        from lies.qmd.access import classify_call_error  # local: access imports this module

        try:
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)
        except Exception as e:
            # Deliberately broad: fastmcp 4 presents a dead session
            # as a bare ``RuntimeError`` with the transport error on
            # ``__cause__``, so a tighter catch would never see the
            # failure the taxonomy exists to classify.
            action, retryable = classify_call_error(e)
            if action == "passthrough":
                raise
            state = await self._do_recycle()
            if not retryable:
                raise ModelRetry(
                    f"qmd daemon wedged on call to {name!r}; recycled (pid {state.pid or 'unknown'})"
                ) from None
            try:
                return await self.wrapped.call_tool(name, tool_args, ctx, tool)
            except Exception as e2:
                retry_action = classify_call_error(e2)[0]
                if retry_action == "passthrough":
                    raise
                still = (
                    "still timing out" if retry_action == "recycle-raise" else "still unreachable"
                )
                raise ToolFailed(f"qmd daemon {still} after recycle: {e2}") from e2

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
