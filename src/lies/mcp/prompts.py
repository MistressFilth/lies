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

Single-string tails
-------------------

Every prompt takes exactly one ``str`` — ``question`` for ``ask``,
``tail`` for the other six. Claude Code's slash-command parser splits
the tail on whitespace and binds tokens to typed prompt parameters in
declared order, which would shred a multi-word question across typed
fields and break JSON binding; one ``str`` slot has no such problem.
The other six parse their own ``--flag[=value]`` vocabulary inside the
body.

One string slot makes the slash path *safe*, not *complete*: the host
binds exactly one token and everything after it is overflow that the
host drops. The multi-token grammar those bodies parse is reachable in
full only through ``get_prompt``, whose tool-call arguments bypass the
slash pre-tokenizer. So ``get_prompt`` is the path for a multi-word
question, and the slash form exercises the single-token subset. See
``src/lies/mcp/instructions.md`` for the routing rule hosts read.

``ask`` and ``ground`` additionally parse ``+tag`` / ``-tag`` filter
markers out of the *leading* run of the question text, so the routed
``mcp__lies__search`` / ``mcp__lies__lib_ask`` calls carry the parsed
``tag_expr`` / ``exclude_tags`` values rather than the raw tail. This
matches the spec's ``argument-hint: "[+tag-expr] [-tag ...] <question>"``
and the ask plugin's existing ``+tag -tag question text`` convention.
The leading run is the guard: a library of command-line tooling is
full of questions *about* option flags (``the -e flag of grep``), and
scanning the whole question for ``+``/``-`` atoms deletes those words
and re-injects them as search filters.

Spec:
``~/code/project-notes/lies/superpowers/specs/2026-09-29-prompts-as-tools-routing-design.md``
(single-string tail collapse recorded in
``2026-09-29-single-string-prompt-tails-design.md``).
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
