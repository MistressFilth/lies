"""source-reader sub-agent: read raw sources, return structured extraction."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from lies.agents.base import make_sub_agent
from pydantic_ai.output import PromptedOutput


class SourceExtraction(BaseModel):
    """Structured extraction from a single raw source.

    All fields default to empty so a partial extraction (one the model
    refuses to fill for non-prose inputs like ``llms.txt`` indexes, or a
    fully-skipped extraction when the agent raises) constructs cleanly
    via ``SourceExtraction()``. The empty defaults also let downstream
    callers return a sensible value when the model hits validation
    retries or HTTP errors — the extraction is advisory and not consumed
    by ``PageWriterDeps``.
    """

    claims: list[str] = []
    """Atomic factual claims made by the source."""

    entities: list[str] = []
    """Named things (people, projects, systems) the source discusses."""

    concepts: list[str] = []
    """Abstract ideas or patterns the source discusses."""

    comparisons: list[tuple[str, str]] = []
    """Pairs of (entity_A, entity_B) that the source compares."""

    summary: str = ""
    """One-paragraph summary of the source.

    Defaults to empty string: link-list sources (``llms.txt`` indexes,
    sitemap excerpts, navigation manifests) have no prose to summarize, and
    forcing the model to fabricate a summary for non-prose inputs produces
    a validation error that triggers pydantic-ai's "exceeded maximum
    output retries" path. Downstream consumers (``PageWriterDeps``) do
    not currently consume this field, so the default is safe.
    """


async def read_file(ctx: RunContext[None], path: str, raw_root: str) -> str:
    """Read a UTF-8 source confined to the wiki's ``raw/`` directory."""
    try:
        root = Path(raw_root).resolve(strict=True)
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        source = candidate.resolve(strict=True)
        source.relative_to(root)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return f"ERROR: source must be an existing file under {raw_root}: {path}"
    if not source.is_file():
        return f"ERROR: source is not a file: {path}"
    try:
        return source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return f"ERROR: could not read {path}: {sys.exc_info()[1]}"


SOURCE_READER_SYSTEM_PROMPT = """Your job is to read a single raw source and
return a structured `SourceExtraction`.

A "source" is a local markdown file or plain text file. The user gives you the
path. Read it carefully and extract:

- **claims**: atomic factual statements (one fact per claim)
- **entities**: named things (people, projects, systems, libraries)
- **concepts**: abstract ideas, patterns, methodologies
- **comparisons**: pairs of things the source compares
- **summary**: a one-paragraph summary

Be precise. Quote exact phrases where the wording matters. If a section is
ambiguous, omit it rather than guess. Do not invent content the source does
not contain.

To load the source, call the `read_file` tool with the path and the wiki
`raw/` root supplied by the orchestrator. Only files inside `raw/` are
readable; tests and wiki pages are intentionally out of scope.
"""


def source_reader_agent(
    model: Model | str = "anthropic:claude-opus-4-7",
    tools: list[Callable[..., Any]] | None = None,
) -> Agent[None, SourceExtraction]:
    """Construct the source-reader sub-agent."""
    # PromptedOutput wraps the schema in instructions and parses the
    # model's free-form JSON text rather than relying on tool calling
    # or response_format=json_schema. MiniMax-M3 ignores tool_choice and
    # response_format on both endpoints; PromptedOutput is the only
    # shape that works reliably with that model. See the project-notes
    # issue that captured the systematic-debugging trace:
    # superpowers/issues/2026-09-07-ingest-design-mismatch.md
    return make_sub_agent(
        model=model,
        output_type=PromptedOutput(SourceExtraction),
        system_prompt=SOURCE_READER_SYSTEM_PROMPT,
        tools=tools if tools is not None else [read_file],
    )
