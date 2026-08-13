"""source-reader sub-agent: read raw sources, return structured extraction."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from lies.agents.base import make_sub_agent


class SourceExtraction(BaseModel):
    """Structured extraction from a single raw source."""

    claims: list[str]
    """Atomic factual claims made by the source."""

    entities: list[str]
    """Named things (people, projects, systems) the source discusses."""

    concepts: list[str]
    """Abstract ideas or patterns the source discusses."""

    comparisons: list[tuple[str, str]]
    """Pairs of (entity_A, entity_B) that the source compares."""

    summary: str
    """One-paragraph summary of the source."""


async def read_file(ctx: RunContext[None], path: str, raw_root: str) -> str:
    """Read a UTF-8 source confined to the wiki's ``raw/`` directory."""
    try:
        root = Path(raw_root).resolve(strict=True)
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        source = candidate.resolve(strict=True)
        source.relative_to(root)
    except FileNotFoundError, OSError, TypeError, ValueError:
        return f"ERROR: source must be an existing file under {raw_root}: {path}"
    if not source.is_file():
        return f"ERROR: source is not a file: {path}"
    try:
        return source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return f"ERROR: could not read {path}: {exc}"


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
    return make_sub_agent(
        model=model,
        output_type=SourceExtraction,
        system_prompt=SOURCE_READER_SYSTEM_PROMPT,
        tools=tools if tools is not None else [read_file],
    )
