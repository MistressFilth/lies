"""Slash-command prompt registrations for the LIES MCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """Bind all seven slash-command prompts to ``mcp``.

    Idempotent at the call site — each ``@mcp.prompt`` decorator is
    applied once at module import time. Calling this entry point a
    second time is a no-op (FastMCP rejects duplicate names; the
    decorators only fire on the first call).
    """
    # Local import — `prompts_impl` triggers FastMCP decorator side
    # effects at module load. Keeping it inside the function defers
    # that until register_prompts is actually called, so the test
    # suite can import `prompts` without spinning up an MCP fixture.
    from lies.mcp.prompts_impl import register_all

    register_all(mcp)
