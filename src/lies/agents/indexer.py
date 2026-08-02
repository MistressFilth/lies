"""indexer sub-agent: maintain wiki/index.md and wiki/log.md."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel
from pydantic_ai import Agent

from lies.agents.base import make_sub_agent


class IndexerResult(BaseModel):
    """The result of an indexer invocation."""

    index_content: str
    """Full new content for `wiki/index.md`."""

    log_entry: str
    """The new log line to append to `wiki/log.md` (without trailing newline)."""


INDEXER_SYSTEM_PROMPT = """Your job is to maintain two special files in the wiki:

1. **`wiki/index.md`** — the content-oriented catalog of every page. Organized
   by page type (entity, concept, comparison, source, overview). Each entry:
   a markdown link, a one-line summary, and optional metadata (date, source count).

2. **`wiki/log.md`** — the chronological append-only log. Each entry starts with
   a parseable prefix: `## [YYYY-MM-DD] <operation> | <title>`. Operations:
   `ingest`, `query`, `lint`.

You receive:
- A list of `PageDiff` objects (from page-writer)
- The current `index.md` content (if any)
- The current `log.md` content (if any)
- The operation type (`ingest` / `query` / `lint`)

Return an `IndexerResult` with:
- `index_content`: the FULL new content of `index.md` (rebuilt from scratch,
  not a diff)
- `log_entry`: the new line to append to `log.md` (no trailing newline)

Rules:
- The index is organized by page type, then alphabetical within each type.
- Each entry in the index is `- [Name](path) — one-line summary.`
- Log entries are sorted by date in the file (newest at bottom).
- The log entry format is parseable: `## [YYYY-MM-DD] ingest | <Title>`
- If the wiki is small (~100 sources, hundreds of pages), the index alone is
  sufficient for navigation. No embedding-based RAG needed.
"""


def indexer_agent(model: str = "anthropic:claude-opus-4-7") -> Agent[None, IndexerResult]:
    """Construct the indexer sub-agent."""
    return make_sub_agent(
        model=model,
        output_type=IndexerResult,
        system_prompt=INDEXER_SYSTEM_PROMPT,
    )


def format_log_entry(operation: str, title: str, when: date | None = None) -> str:
    """Format a log entry with the parseable prefix."""
    when = when or date.today()  # noqa: DTZ011 - log dates use the local calendar date
    return f"## [{when.isoformat()}] {operation} | {title}"
