"""Slash-command prompt registrations for the LIES MCP server.

Seven ``@mcp.prompt`` handlers cover the user-invokable entry points:

- ``ask``: synthesized cited answer
- ``collections``: library registry CRUD via ``mcp__lies__collections_read``
  and library helpers
- ``ingest``: route a source into the library via ``Bash(lies ingest ...)``
- ``lint``: health-check via ``mcp__lies__lint``
- ``reindex``: rebuild search index via ``mcp__lies__reindex``
- ``sync``: pull + ingest remotes via ``Bash(lies sync ...)``
- ``ground``: cite-snippet digest via ``mcp__lies__search`` + ``mcp__lies__read``

Each handler returns a single user-role ``Message`` carrying the
templated body. No LLM subagent dispatch happens from the prompt — the
LLM reads the body and calls the routed tools directly in the same
turn.

Spec:
``~/code/project-notes/lies/superpowers/specs/2026-09-28-slash-prompt-surface-design.md``.
"""

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
