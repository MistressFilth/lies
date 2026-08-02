"""page-writer sub-agent: create or update wiki pages per the schema."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent

from lies.agents.base import SUB_AGENT_SYSTEM_PROMPT_PREFIX


class PageOperation(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class PageDiff(BaseModel):
    """A proposed change to a single wiki page."""

    path: Path
    """Path relative to the wiki root, e.g., 'wiki/entities/postgres.md'."""

    operation: PageOperation

    old_content: str | None = None
    """Existing content (for UPDATE); None for CREATE."""

    new_content: str | None = None
    """Proposed new content (for CREATE/UPDATE); None for DELETE."""


PAGE_WRITER_SYSTEM_PROMPT = """Your job is to create or update wiki pages
based on extracted source material.

You receive:
- An `extraction` (from source-reader) describing what the source contains
- A list of existing pages (so you don't duplicate or contradict)
- The wiki schema (page types, frontmatter, conventions)

Return a list of `PageDiff` objects. For each one:
- `path`: relative to wiki root, e.g., `wiki/entities/postgres.md`
- `operation`: CREATE / UPDATE / DELETE
- `old_content`: existing content (UPDATE only)
- `new_content`: proposed new content (CREATE/UPDATE)

Rules:
- One page per entity or concept (no duplicates).
- Always include YAML frontmatter (title, type, tags, created, updated, sources).
- Add cross-references (`[Name](entities/name.md)`) liberally.
- When updating, preserve valid existing content; integrate new information.
- Cite sources at the bottom of each page.
- Do not touch `wiki/index.md` or `wiki/log.md` — that's the indexer's job.
"""


def page_writer_agent(model: str = "anthropic:claude-opus-4-7") -> Agent[None, list[PageDiff]]:
    """Construct the page-writer sub-agent."""
    return Agent(
        model,
        output_type=list[PageDiff],
        system_prompt=SUB_AGENT_SYSTEM_PROMPT_PREFIX + PAGE_WRITER_SYSTEM_PROMPT,
    )
