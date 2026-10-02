"""qmd MCP client.

The qmd MCP server exposes `query`, `get`, `multi_get`, `status` tools
that the LIES orchestrator uses for hybrid search.

Usage:
    from pydantic_ai import Agent
    from lies.qmd.mcp import QmdMcpClient

    qmd = QmdMcpClient(transport="stdio")
    agent = Agent("<configured-model>", capabilities=[qmd.as_capability()])

LIES no longer hard-codes a vendor-default model; operators must
configure a model via providers.toml or LIES_<AGENT>_MODEL before
constructing an agent.

Note: the stdio transport needs pydantic-ai's `mcp` extra installed at
runtime — `pip install "pydantic-ai-slim[mcp]"`. Without it the capability
constructs fine but tool calls will fail with an ImportError explaining
the missing dependency.

For stdio the qmd binary is launched as `qmd mcp` through a FastMCP
`StdioTransport`, which is the invocation required for qmd's MCP server.

`QmdRecycleToolset` is the *agent* face of qmd access. The library face
— one seam every LIES read goes through, and the classification of a
failed call — is `lies.qmd.access`; this module re-wraps that
classification in pydantic-ai's `ModelRetry`/`ToolFailed`, which stop
here.

See https://github.com/tobi/qmd#mcp for the qmd MCP surface.
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

try:
    from pydantic_ai.mcp import MCPToolset  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — pydantic_ai[mcp] is a runtime extra
    MCPToolset: Any = None

if TYPE_CHECKING:
    from lies.qmd.daemon import QmdState  # noqa: F401

_log = logging.getLogger(__name__)


def _default_read_timeout_s() -> float:
    """The read deadline for a daemon call, in seconds.

    Read at call time from :func:`lies.config.get_qmd_query_timeout` —
    the single source for every qmd retrieval deadline, including the
    CLI subprocess paths — so the daemon and the CLI cannot end up with
    different answers for the same operation. The connect/write/pool
    values stay literal: they bound the handshake and the request write,
    not the retrieval.
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

    The qmd daemon's first HyDE query after fresh start can wedge the
    call handler for ~60 s (qexpander cold-start latency, not event-loop
    deadlock; see spec §"Problem"). The default fastmcp http transport
    uses no explicit timeouts, so a wedged daemon surfaces as a read
    timeout only after whatever httpx considers "infinite." Bounding the
    read keeps the wedge to a single recycle round-trip.

    ``**kwargs`` and the ``Any`` annotations are not laziness — fastmcp
    calls this factory with ``follow_redirects=True`` (and may add
    arguments in a future release), and the previous fixed signature
    made *every* HTTP daemon call fail at connect with a ``TypeError``
    before the recycle taxonomy could run at all.

    The client is built from the httpx module fastmcp actually
    installed, which as of 4.0 is its own vendored ``httpx2``; a
    client from the other generation is not the type the transport
    expects to hold. If that vendored name ever goes away, fall back to
    ``httpx`` rather than failing the import — a wrong-but-connectable
    client is recoverable, a module that will not import is not.
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
                httpx_client_factory=_build_qmd_httpx_client,
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

    The *classification* of a failure is not this class's business — it
    lives in :func:`lies.qmd.access.classify_call_error`, which answers
    ``(action, retryable)`` for any qmd call regardless of who made it.
    What is this class's business is the re-wrapping: a model has to be
    *told* a tool failed and given the chance to decide what to do
    (``ModelRetry``), where a library caller wants an exception. Both
    are agent-path concerns, and both stop here — ``ModelRetry`` never
    escapes into the library path.

    The three failure modes (the shape matches ask's
    ``_post_query_locked`` reference at
    ask/repo/ask/scripts/ask.py:965-981):

    - wedge — the daemon accepted the call and stopped answering:
      recycle + raise ``ModelRetry``. The same payload would re-wedge
      the fresh daemon, so there is no transparent retry; the model
      sees the failed result and decides.
    - unreachable — the daemon was not there: recycle + retry once. If
      the retry also fails, raise ``ToolFailed`` so the model sees a
      terminal failure with the real reason.
    - not ours — a protocol-level rejection (e.g. ``-32602 Invalid
      params``): re-raise unchanged. Recycling cannot change the
      daemon's answer to a malformed request.

    Which of the three a given exception is lives in
    ``classify_call_error``, not here. Recycle failure
    (``QmdRecycleFailed`` from ``recycle_qmd_daemon``) surfaces as
    ``ToolFailed("qmd daemon recycled but never served")``.

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
        from lies.qmd.access import classify_call_error  # local: access imports this module

        try:
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)
        except Exception as e:
            # This catch is deliberately broad, and is the one place the
            # agent path widens: fastmcp 4 presents a dead session as a
            # bare `RuntimeError` with the transport error on
            # `__cause__`, so a `(httpx.TransportError, mcp.MCPError)`
            # catch would never see the failure the taxonomy exists to
            # classify. `classify_call_error` returns "passthrough" for
            # anything it does not own, and that re-raises unchanged —
            # so the observable behaviour for every non-transport error
            # is identical to before.
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
                # The retry's failure is classified, not assumed. A
                # second failure the taxonomy does not own — a decode
                # error, a malformed result — propagates rather than
                # becoming a `ToolFailed` claiming the daemon is
                # unreachable, which it was not. The two wordings are
                # the ones this class has always used: a retry that
                # timed out is a daemon that is up and not answering.
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
