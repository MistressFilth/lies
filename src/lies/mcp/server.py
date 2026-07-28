"""FastMCP server exposing LIES as MCP tools, resources, and a prompt.

The server speaks stdio only (v1). Tools are thin wrappers around the
existing ``Orchestrator`` API; resources read raw markdown via
``WikiLayout``. See ``docs/superpowers/specs/2026-07-27-lies-mcp-design.md``
for the full surface.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel

from lies import __version__
from lies.mcp.resolution import (
    WikiRootError,
    _resolve_wiki_root,
)
from lies.orchestrator import Orchestrator
from lies.query.models import SynthesizedAnswer
from lies.schema.loader import load_default_schema
from lies.wiki.layout import WikiLayout

mcp = FastMCP("lies")


class SynthesizedMcpAnswer(BaseModel):
    """Structured answer returned by the ``query`` tool.

    Maps from the internal ``SynthesizedAnswer`` (drops citations,
    pages_read, page_links — those are available via the wiki://
    resources if the LLM wants them).
    """

    answer: str
    fallback_used: bool
    fallback_reason: str | None  # None when qmd served the query


# ---------------------------------------------------------------------------
# init_wiki — bootstrap a new wiki
# ---------------------------------------------------------------------------


@mcp.tool
def init_wiki(path: str) -> str:
    """Initialize a new LIES wiki at ``path``.

    Creates the raw/, wiki/, .lies/ directories, copies the default
    schema to .lies/schema.md, runs ``git init``, and makes an initial
    commit. The path must not already contain files.
    """
    target = Path(path).expanduser().resolve()
    if target.exists():
        if not target.is_dir():
            raise WikiRootError(f"{target} is not a directory")
        if any(target.iterdir()):
            raise WikiRootError(f"{target} is not empty")

    target.mkdir(parents=True, exist_ok=True)
    layout = WikiLayout(target)
    layout.init()
    layout.schema_path.write_text(load_default_schema(), encoding="utf-8")
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(target)],
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "lies@local"],
        cwd=target, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "LIES"],
        cwd=target, check=True,
    )
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit: empty LIES wiki"],
        cwd=target, check=True,
    )
    return f"Initialized LIES wiki at {target} (v{__version__})"


# ---------------------------------------------------------------------------
# ingest_source — atomic ingest through the orchestrator
# ---------------------------------------------------------------------------


@mcp.tool
def ingest_source(source: str, wiki_root: str | None = None) -> str:
    """Ingest ``source`` into the wiki at ``wiki_root``.

    Delegates to ``Orchestrator.run_ingest``, which snapshots the
    working tree, runs the agent, and commits atomically. On any
    failure the working tree is restored and the exception is
    re-raised.
    """
    layout = _resolve_wiki_root(wiki_root)
    orch = Orchestrator(wiki_root=layout.root)
    return orch.run_ingest(source)


# ---------------------------------------------------------------------------
# query — extractive answer with structured fallback metadata
# ---------------------------------------------------------------------------


@mcp.tool
def query(question: str, wiki_root: str | None = None) -> SynthesizedMcpAnswer:
    """Answer ``question`` from the wiki at ``wiki_root``.

    Uses the qmd → index.md fallback path. The result always has
    ``fallback_used`` and ``fallback_reason`` populated so the caller
    can distinguish a real qmd-backed answer from a fallback.
    """
    layout = _resolve_wiki_root(wiki_root)
    orch = Orchestrator(wiki_root=layout.root)
    ans: SynthesizedAnswer = orch.run_query(question)
    return SynthesizedMcpAnswer(
        answer=ans.answer,
        fallback_used=ans.fallback_used,
        fallback_reason=ans.fallback_reason or None,
    )


# ---------------------------------------------------------------------------
# lint — deterministic health-check
# ---------------------------------------------------------------------------


@mcp.tool
def lint(wiki_root: str | None = None, fix: bool = False) -> str:
    """Run the lint pass and write ``wiki/lint-report.md``.

    ``fix`` is accepted for parity with the CLI; safe-fix application
    is reserved for the linter sub-agent and is not yet exposed over
    MCP.
    """
    del fix  # reserved for future use
    layout = _resolve_wiki_root(wiki_root)
    orch = Orchestrator(wiki_root=layout.root)
    return orch.run_lint()
