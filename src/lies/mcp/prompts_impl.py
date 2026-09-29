"""Prompt implementations — module-level functions decorators wrap.

Each ``<name>_prompt`` function in this module returns a list of
``Message`` for the prompt it implements. ``register_all`` decorates
each one with ``@mcp.prompt(name=...)`` and binds it to the live
``mcp``. Tests drive the impl functions directly via
``prompts_impl.<name>_prompt(...)`` without spinning up an MCP
instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.prompts import Message  # noqa: F401  (re-exported for impl fns)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_all(mcp: FastMCP) -> None:
    """Wire each impl function as ``@mcp.prompt`` on ``mcp``."""

    @mcp.prompt(
        name="ask",
        description="Synthesized cited answer to a question.",
    )
    def _ask_prompt(
        question: str,
        tag_expr: str | None = None,
        exclude_tags: list[str] | None = None,
    ) -> list[Message]:
        return ask_prompt(question, tag_expr, exclude_tags)

    @mcp.prompt(
        name="ground",
        description="Cite-snippet digest (no synthesis).",
    )
    def _ground_prompt(
        question: str,
        tag_expr: str | None = None,
        exclude_tags: list[str] | None = None,
        top_k: int = 3,
    ) -> list[Message]:
        return ground_prompt(question, tag_expr, exclude_tags, top_k)


# Concrete impl functions added by tasks 3-9 below.


def ask_prompt(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
) -> list[Message]:
    """Synthesized cited answer to a question."""
    body = (
        f"Call mcp__lies__search({question!r}, tag_expr={tag_expr!r}, "
        f"exclude_tags={exclude_tags!r}) to find hits. "
        f"Read each top-ranked page body via mcp__lies__read([path]). "
        f"Then call mcp__lies__lib_ask({question!r}, "
        f"tag_expr={tag_expr!r}, exclude_tags={exclude_tags!r}) for "
        f"a synthesized cited answer. "
        f'Cite each claim as [[collection/slug]]: "verbatim quote from the cited span".'
    )
    return [Message(body)]


def ground_prompt(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
    top_k: int = 3,
) -> list[Message]:
    """Cite-snippet digest (no synthesis)."""
    body = (
        f"Call mcp__lies__search({question!r}, tag_expr={tag_expr!r}, "
        f"exclude_tags={exclude_tags!r}) to find hits. "
        f"Read each top-ranked page body via mcp__lies__read([path]). "
        f"Render each citation as "
        f'[[collection/slug]] (Title): "≤200-char verbatim snippet" '
        f"(clamped to top_k={top_k} entries). "
        f"Cite marker is grounded in the read span's body, not synthesized prose. "
        f"Do NOT route through lib_ask — ground is digest-only."
    )
    return [Message(body)]
