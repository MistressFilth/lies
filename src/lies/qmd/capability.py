"""Daemon-aware capability for the agent's qmd tool surface.

By default the agent advertises the native qmd MCP server (the shared
qmd daemon at ``LIES_QMD_URL``). When the daemon is unreachable at
construction time, the capability advertises an in-process
:class:`QmdFallbackMcp` server instead and prints a single stderr
warning naming the URL and the fix.

The probe runs every :meth:`as_capability` call, so the capability
transparently flips back to native if the daemon comes online.

The capability is implemented as a thin wrapper around
:class:`pydantic_ai.capabilities.MCP` — we don't subclass `MCP`
because `MCP` is the public primitive for "native-or-local" already,
and we only vary which arguments we pass it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
import logging


import fastmcp
import httpx
import mcp
from pydantic_ai.capabilities import MCP

from lies.qmd.health import qmd_daemon_reachable
from lies.qmd.mcp import QmdRecycleToolset, _build_qmd_http_toolset

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from lies.wiki.wiki import Wiki


_DEFAULT_TIMEOUT_S = 0.5
# TCP-connect probe: a half-second is enough to confirm a listener exists on
# the qmd daemon port (connect is a fast, binary yes/no — 0.5s vs the 60s
# JSON-RPC read budget is a 120× gap, justified because the read budget covers
# the wedge *after* connect where the daemon must answer ``list_tools``).


# Errors the construction-time liveness probe (a fastmcp.Client
# session + list_tools() round-trip) treats as "daemon not actually
# serving", which trips the in-process fallback. Broader than what
# :func:`recycle_qmd_daemon` catches because the probe does not
# retry — any one error means we cannot advertise the native toolset.
_MCP_PROBE_ERRORS: tuple[type[BaseException], ...] = (
    mcp.MCPError,
    OSError,
)


class QmdCapability:
    """Decides which `MCP(...)` shape the agent should advertise."""

    def __init__(
        self,
        wiki: Wiki,
        *,
        transport: str,
        url: str = "http://127.0.0.1:8181",
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        if transport not in {"stdio", "http"}:
            raise ValueError(f"Unknown transport: {transport}")
        self._transport = transport
        self._url = url
        self._wiki = wiki
        # ``WikiLayout`` exposes ``root``; ``Wiki`` exposes ``data_root``.
        # Both name the on-disk path the qmd sidecar belongs to. Pick
        # whichever exists; the second ``getattr`` is lazy because the
        # first already short-circuits when ``data_root`` is present.
        _data_root = getattr(wiki, "data_root", None) or getattr(wiki, "root", None)
        self._data_dir: Path = cast("Path", _data_root)
        self._timeout = timeout

    def as_capability(self) -> MCP:
        if self._transport == "stdio":
            return _build_stdio_mcp()
        if not qmd_daemon_reachable(self._url, timeout=self._timeout):
            _warn_degraded(self._url)
            return _build_fallback_mcp(self._wiki)
        try:
            return _build_native_mcp(self._url, self._data_dir)
        except (httpx.HTTPError, *_MCP_PROBE_ERRORS):
            _warn_degraded(self._url)
            return _build_fallback_mcp(self._wiki)


def _build_native_mcp(url: str, data_dir: Path) -> MCP:
    """MCP wrapping a QmdRecycleToolset around an inner MCPToolset.

    Construction is non-destructive: a fastmcp liveness probe (a
    one-shot ``Client.list_tools()`` round-trip) confirms the daemon
    is actually serving before we advertise the native toolset. If
    the probe fails we raise and the caller falls back to the
    in-process :class:`QmdFallbackMcp`. The actual reap+spawn path
    lives in :class:`QmdRecycleToolset.call_tool` — that is where
    per-call recovery belongs.

    Args:
        url: The qmd daemon's HTTP base URL.
        data_dir: The on-disk wiki root; passed to the recycle callback
            so a per-call recycle can write the sidecar correctly.

    Raises:
        httpx.HTTPError: probe transport failure.
        mcp.MCPError: probe protocol-level rejection.
        OSError: probe transport-level failure (e.g. refused connection).
    """
    # ``TYPE_CHECKING`` keeps the fastmcp import out of the module
    # top; the probe constructs a real client here so a daemon-side
    # failure surfaces to the caller, not to module import.
    inner = _build_qmd_http_toolset(url)

    async def _recycle() -> Any:
        from lies.qmd.daemon import recycle_qmd_daemon

        return await recycle_qmd_daemon(data_dir=data_dir, daemon_url=url)

    # Synchronous construction-time liveness probe. The TCP probe
    # in :meth:`as_capability` only confirms a listener exists; this
    # proves the daemon actually serves a JSON-RPC session. The probe
    # runs synchronously via ``asyncio.run``; any async caller MUST
    # construct the capability outside the loop (``asyncio.run``
    # raises ``RuntimeError`` when an event loop is already running).
    asyncio.run(_probe_liveness(url))

    wrapped = QmdRecycleToolset(wrapped=inner, recycle_cb=_recycle)
    return MCP(
        local=lambda: wrapped,
        id="lies.qmd",
        description=(
            "qmd MCP daemon. Search the wiki, read pages, and check "
            "collection status. Auto-recycles on transport errors; "
            "falls back to the in-process index scan when the daemon "
            "cannot be restarted."
        ),
    )


async def _probe_liveness(url: str) -> None:
    """Open a one-shot fastmcp.Client + call ``list_tools`` to prove
    the daemon is actually serving. Raises on transport / protocol /
    OS errors so the caller can fall back to the in-process toolset.
    """
    async with fastmcp.Client(url) as client:  # type: ignore[attr-defined]
        await client.list_tools()


def _build_fallback_mcp(wiki: Wiki) -> MCP:
    """MCP(local=factory) — agent sees the in-process FastMCP fallback."""

    def _factory() -> Any:
        from lies.qmd.mcp_fallback import QmdFallbackMcp

        server = QmdFallbackMcp(wiki)
        return server.as_toolset()

    return MCP(
        local=_factory,
        id="lies.qmd",
        description=(
            "qmd-backed wiki search and page read tools. "
            "Falls back to a degraded in-process index scan when the qmd "
            "daemon is unreachable; every fallback result carries "
            "degraded=True."
        ),
    )


def _build_stdio_mcp() -> MCP:
    """LIES_QMD_TRANSPORT=stdio — old subprocess-per-agent behavior."""

    def _factory() -> Any:
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport
        from pydantic_ai.mcp import MCPToolset

        client = Client(StdioTransport(command="qmd", args=["mcp"]))
        return MCPToolset(client)

    return MCP(local=_factory, id="lies.qmd.stdio")


def _warn_degraded(url: str) -> None:
    """One stderr line, operator-meaningful, names the URL and the fix."""
    print(
        f"warning: qmd daemon unreachable at {url}; "
        f"wiki search is running degraded (in-process index scan). "
        f"Set LIES_QMD_URL or run 'qmd mcp --http --daemon' to restore "
        f"the healthy path.",
        file=sys.stderr,
    )
