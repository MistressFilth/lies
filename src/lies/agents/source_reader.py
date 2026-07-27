"""source-reader sub-agent: read raw sources, return structured extraction."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

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


async def read_file(ctx: RunContext[None], path: str) -> str:
    """Read a local source file and return its content as a string.

    Use this to load a markdown or text source the user pointed at.
    Returns an `ERROR: ...` line if the file is missing or unreadable.
    """
    try:
        p = Path(path)
    except (TypeError, ValueError) as exc:
        return f"ERROR: invalid path: {path!r} ({exc})"
    if not p.is_file():
        return f"ERROR: file not found: {path}"
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
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

To load the source, call the `read_file` tool with the path the user gave you.
URL and PDF support is a future enhancement; for now the user must ingest
local files only.
"""


def source_reader_agent(
    model: str = "anthropic:claude-opus-4-7",
    tools: list[Callable[..., Any]] | None = None,
) -> Agent[None, SourceExtraction]:
    """Construct the source-reader sub-agent."""
    return make_sub_agent(
        model=model,
        output_type=SourceExtraction,
        system_prompt=SOURCE_READER_SYSTEM_PROMPT,
        tools=tools if tools is not None else [read_file],
    )
