"""FastMCP server exposing LIES as MCP tools, resources, and a prompt.

The server speaks stdio only (v1). Tools are thin wrappers around the
existing ``Orchestrator`` API; resources read raw markdown via
``WikiLayout``. See ``docs/superpowers/specs/2026-07-27-lies-mcp-design.md``
for the full surface.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

from fastmcp import FastMCP
from pydantic import BaseModel

from lies import __version__
from lies.mcp.resolution import (
    WikiRootError,
    _resolve_wiki_root,
    _safe_page_path,
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
    citations: list[str]
    pages_read: list[str]
    changed_pages: list[str]


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
# wiki_search / wiki_read — direct memory retrieval
# ---------------------------------------------------------------------------


@mcp.tool
def wiki_search(
    question: str,
    collection_ids: list[str] | None = None,
    limit: int = 5,
    wiki_root: str | None = None,
) -> dict[str, object]:
    """Search the wiki at ``wiki_root`` for project knowledge."""
    from lies.memory.service import WikiMemoryService

    layout = _resolve_wiki_root(wiki_root)
    result = WikiMemoryService(layout).search(
        question,
        collection_ids=collection_ids,
        limit=limit,
    )
    return cast(dict[str, object], result.model_dump())


@mcp.tool
def wiki_read(
    page_ids: list[str],
    wiki_root: str | None = None,
) -> dict[str, str]:
    """Read full wiki pages by ID for the wiki at ``wiki_root``."""
    from lies.memory.service import WikiMemoryService

    layout = _resolve_wiki_root(wiki_root)
    return WikiMemoryService(layout).read(page_ids)


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
        citations=ans.citations,
        pages_read=ans.pages_read,
        changed_pages=ans.changed_pages,
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


# ---------------------------------------------------------------------------
# Resources — raw wiki reads, no LLM round-trip
# ---------------------------------------------------------------------------
#
# Each resource is defined as an ``_impl`` function (takes ``wiki_root``,
# does the real work) plus a thin zero-argument forwarder that FastMCP
# registers as the static-resource handler. The forwarder pattern is a
# FastMCP 3.4.5 constraint: static-resource handlers must be
# zero-argument. Keeping the impl beside the forwarder lets tests and
# direct callers exercise the real logic with an explicit ``wiki_root``.


def _wiki_status_impl(wiki_root: str | None = None) -> str:
    """Return qmd status plus the last 10 lines of ``wiki/log.md``.

    If qmd is unavailable, the error is embedded in the returned string
    (resource reads must never fail loudly for a degraded-but-functional
    state).
    """
    layout = _resolve_wiki_root(wiki_root)
    out = "=== qmd status ===\n"
    try:
        from lies.qmd import qmd_status as _qmd_status

        out += _qmd_status(layout.root)
    except Exception as exc:  # noqa: BLE001 - degraded path must surface, not crash
        out += f"qmd unavailable: {exc}"
    out += "\n\n=== last 10 log entries ===\n"
    if layout.log_path.exists():
        lines = layout.log_path.read_text(encoding="utf-8").splitlines()
        for line in lines[-10:]:
            out += line + "\n"
    else:
        out += "(no log yet)\n"
    return out


@mcp.resource("wiki://status")
def wiki_status() -> str:
    """qmd status + last 10 log lines.

    Zero-argument forwarder (FastMCP 3.4.5 constraint on static-resource
    handlers). Real logic in :func:`_wiki_status_impl`; ``wiki_root`` is
    resolved from the env / cwd there.
    """
    return _wiki_status_impl()


def _wiki_index_impl(wiki_root: str | None = None) -> str:
    layout = _resolve_wiki_root(wiki_root)
    if not layout.index_path.exists():
        return ""
    return layout.index_path.read_text(encoding="utf-8")


@mcp.resource("wiki://index")
def wiki_index() -> str:
    """Raw contents of ``wiki/index.md`` (empty string if absent).

    Zero-argument forwarder (FastMCP 3.4.5 constraint). Real logic in
    :func:`_wiki_index_impl`; ``wiki_root`` is resolved from the env /
    cwd there.
    """
    return _wiki_index_impl()


def _wiki_log_impl(wiki_root: str | None = None) -> str:
    layout = _resolve_wiki_root(wiki_root)
    if not layout.log_path.exists():
        return ""
    return layout.log_path.read_text(encoding="utf-8")


@mcp.resource("wiki://log")
def wiki_log() -> str:
    """Raw contents of ``wiki/log.md`` (empty string if absent).

    Zero-argument forwarder (FastMCP 3.4.5 constraint). Real logic in
    :func:`_wiki_log_impl`; ``wiki_root`` is resolved from the env /
    cwd there.
    """
    return _wiki_log_impl()


def _wiki_lint_report_impl(wiki_root: str | None = None) -> str:
    layout = _resolve_wiki_root(wiki_root)
    if not layout.lint_report_path.exists():
        return ""
    return layout.lint_report_path.read_text(encoding="utf-8")


@mcp.resource("wiki://lint-report")
def wiki_lint_report() -> str:
    """Raw contents of ``wiki/lint-report.md`` (empty string if absent).

    Zero-argument forwarder (FastMCP 3.4.5 constraint). Real logic in
    :func:`_wiki_lint_report_impl`; ``wiki_root`` is resolved from the
    env / cwd there.
    """
    return _wiki_lint_report_impl()


def _wiki_page_impl(path: str, wiki_root: str | None = None) -> str:
    """Return the raw markdown of any page under ``wiki/``.

    ``path`` is relative to ``<wiki_root>/wiki/``. Absolute paths,
    ``..`` traversal, and any path that resolves outside the wiki are
    rejected with ``WikiRootError``. Missing files return ``""``
    (the resource exists; the page just hasn't been written yet).
    """
    layout = _resolve_wiki_root(wiki_root)
    resolved = _safe_page_path(layout.root, path)
    if not resolved.exists():
        return ""
    return resolved.read_text(encoding="utf-8")


@mcp.resource("wiki://page/{path}")
def wiki_page(path: str) -> str:
    """Raw markdown of any page under ``wiki/`` (relative ``path``).

    Template-resource handler — unlike the static resources above,
    FastMCP passes ``path`` directly so the forwarder forwards it to
    :func:`_wiki_page_impl`.
    """
    return _wiki_page_impl(path)


# ---------------------------------------------------------------------------
# Prompt — starter template for asking the wiki
# ---------------------------------------------------------------------------


@mcp.prompt
def ask_wiki(question: str) -> str:
    """Starter prompt that templates a ``query`` tool invocation.

    The LLM receives this prompt and is expected to call the ``query``
    tool with the templated question, then synthesize a final answer
    from the structured result.
    """
    return (
        f"Use the `query` tool to ask the wiki the following question, "
        f"then answer concisely from the structured result:\n\n"
        f"  question: {question}\n\n"
        f"If the result's `fallback_used` is true, mention that the "
        f"answer came from the index fallback (not qmd search) and "
        f"include the `fallback_reason`."
    )
