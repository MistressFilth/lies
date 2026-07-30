"""Pydantic AI read tool adapters for invisible wiki memory.

The main agent receives two read-only tools:

- ``wiki_search(question, limit=5)`` returns bounded evidence.
- ``wiki_read(page_ids)`` returns the markdown body for the given
  page IDs, rejecting unknown IDs.

Both tools depend on :class:`WikiMemoryDeps` so the orchestrator can
inject the service per wiki.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, RunContext

from lies.memory.models import WikiPageNotFound
from lies.memory.retrieval import _path_for_id, read_pages, search_wiki
from lies.memory.service import WikiMemoryService
from lies.wiki.layout import WikiLayout


@dataclass
class WikiMemoryDeps:
    """Per-run dependencies for the main agent's wiki read tools."""

    layout: WikiLayout
    service: WikiMemoryService


def wiki_search_tool(
    ctx: RunContext[WikiMemoryDeps],
    question: str,
    limit: int = 5,
) -> dict[str, object]:
    """Search the wiki for project knowledge relevant to ``question``."""
    deps = ctx.deps
    result = search_wiki(deps.layout, question, limit=limit)
    return result.model_dump()


def wiki_read_tool(
    ctx: RunContext[WikiMemoryDeps],
    page_ids: list[str],
) -> dict[str, str]:
    """Read full page bodies for the given page IDs.

    Rejects any ``page_id`` that does not correspond to a current wiki
    page: the tool contract is "IDs only as returned by ``wiki_search``",
    so a model-supplied raw path or stale id is refused up front rather
    than silently dropped.
    """
    deps = ctx.deps
    unknown = [pid for pid in page_ids if _path_for_id(deps.layout, pid) is None]
    if unknown:
        raise WikiPageNotFound(f"unknown page_ids: {unknown}")
    try:
        return read_pages(deps.layout, page_ids)
    except WikiPageNotFound as exc:
        raise ValueError(str(exc)) from exc


def register_read_tools(agent: Agent[WikiMemoryDeps, Any]) -> None:
    """Register ``wiki_search`` and ``wiki_read`` on ``agent``."""
    agent.tool(
        name="wiki_search",
        description=(
            "Search the wiki for project knowledge relevant to the user's question. "
            "Use when the question may reference project-specific facts, source claims, "
            "concepts, or prior wiki knowledge. Skip for unrelated generic questions. "
            "Returns bounded evidence with page_id values that you can pass to wiki_read."
        ),
    )(wiki_search_tool)
    agent.tool(
        name="wiki_read",
        description=(
            "Read the full markdown body of wiki pages identified by page_id. "
            "Accepts only IDs returned by a recent wiki_search call."
        ),
    )(wiki_read_tool)
