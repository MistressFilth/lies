"""page-writer sub-agent: create or update wiki pages per the schema."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.tools import RunContext

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


PAGE_WRITER_SYSTEM_PROMPT = """You are the page-writer sub-agent. Your job is to create or update wiki pages
based on extracted source material.

You receive:
- An `extraction` (from source-reader) describing what the source contains
- All page paths use the `wiki/<collection>/<file>` convention; retain the explicit `wiki/` prefix.
- A list of existing pages (so you don't duplicate or contradict):
  each line is `- <wiki-relative path>: <one-line summary>`
- The wiki schema (page types, frontmatter, conventions)

Return a list of `PageDiff` objects. For each one:
- `path`: relative to wiki root, e.g., `wiki/claude-code/concepts/hooks.md`.
  Always include the `wiki/` prefix and the per-collection subdir.
- `operation`: CREATE / UPDATE / DELETE
- `old_content`: existing content (UPDATE only)
- `new_content`: proposed new content (CREATE/UPDATE)

Rules:
- One page per entity or concept (no duplicates).
- Always include YAML frontmatter
  (title, type, tags, created, updated, sources, optional derived_from).
- Add cross-references (`[Name](concepts/name.md)`) liberally.
- When updating, preserve valid existing content; integrate new information.
- Cite sources at the bottom of each page.
- Do not touch `wiki/index.md` or `wiki/log.md` — the catalog port owns
  `index.md` (deterministic sqlite-backed mirror) and the orchestrator
  appends `log.md` directly.
- Do not emit DELETE operations — the single-source ingest path is
  write-new. Deletes are reserved for explicit maintenance flows.
"""


@dataclass
class PageWriterDeps:
    """Dependencies the page-writer agent receives per run."""

    question: str
    """Operator-facing ingest prompt (may be empty for boilerplate ingests)."""

    schema_text: str
    """Rendered contents of the wiki's schema markdown."""

    existing_pages: list[tuple[str, str]]
    """Pairs of ``(wiki-relative path, one-line summary)`` for dedup."""


def _build_page_writer_prompt(ctx: RunContext[PageWriterDeps] | Any) -> str:
    """Render the static + dynamic prompt for the page-writer agent.

    Mirrors ``_build_query_prompt``: deps are NOT auto-serialized into
    the message stream, so we render them into the system prompt at
    run time.
    """
    parts: list[str] = [PAGE_WRITER_SYSTEM_PROMPT]
    if ctx.deps is None:
        return parts[0]
    parts.append(f"\nOperator prompt: {ctx.deps.question or '(none)'}")
    parts.append("\nSchema for this wiki:")
    parts.append(ctx.deps.schema_text)
    if ctx.deps.existing_pages:
        parts.append("\nExisting pages (avoid duplicates, preserve cross-links):")
        for rel, summary in ctx.deps.existing_pages:
            parts.append(f"- {rel}: {summary}")
    return "\n".join(parts)


def page_writer_agent(
    model: Model | str = "anthropic:claude-opus-4-7",
) -> Agent[PageWriterDeps, list[PageDiff]]:
    """Construct the page-writer sub-agent."""
    agent: Agent[PageWriterDeps, list[PageDiff]] = Agent(
        model,
        output_type=list[PageDiff],
        system_prompt=SUB_AGENT_SYSTEM_PROMPT_PREFIX + PAGE_WRITER_SYSTEM_PROMPT,
        deps_type=PageWriterDeps,
    )
    agent.system_prompt(_build_page_writer_prompt)
    return agent


def _build_page_writer_prompt_for_test(
    deps: PageWriterDeps | None,
) -> str:
    """Render the prompt without needing a RunContext. Test helper."""
    sentinel = type("_Ctx", (), {"deps": deps})()
    return _build_page_writer_prompt(sentinel)
