"""FastMCP server exposing LIES as MCP tools, resources, and a prompt.

The server speaks stdio only (v1). Tools are thin wrappers around the
existing ``Orchestrator`` API; resources read raw markdown via the
``Wiki`` accessor interface. See
``docs/superpowers/specs/2026-07-27-lies-mcp-design.md`` for the full
surface.

All tools take a wiki ``name`` (or ``None`` to use the env-default) and
resolve it through :func:`lies.mcp.resolution.resolve_wiki` to a
:class:`Wiki` with role-routed XDG paths. ``init_wiki`` is the only
tool that creates a wiki; every other tool/resouce requires the wiki
to already be registered under ``$LIES_XDG_DATA_HOME``.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from fastmcp import FastMCP
from pydantic import BaseModel

from lies import __version__, xdg
from lies.errors import WikiAlreadyExists
from lies.mcp.resolution import resolve_wiki
from lies.memory.models import WikiPlanInvalid
from lies.orchestrator import Orchestrator
from lies.query.models import SynthesizedAnswer
from lies.wiki.layout import WikiLayout, copy_default_schema, git_init_initial
from lies.wiki.wiki import Wiki

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
def init_wiki(name: str) -> dict[str, object]:
    """Initialize a new LIES wiki under ``$LIES_XDG_DATA_HOME/lies/<name>``.

    Creates the five XDG role directories (data, config, cache, state,
    runtime), copies the default schema to ``wiki.config_root/schema.md``,
    runs ``git init`` in ``wiki.data_root``, and makes an initial
    commit. The wiki name must not already be registered.

    Args:
        name: The wiki name. Validated against the same rules as ``Wiki``
            (no path separators, no leading dot, etc.).

    Returns:
        A dict with the wiki's name and the five role-root paths.
    """
    wiki = Wiki(
        name=name,
        data_root=Wiki.data_root_for(name),
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    if wiki.data_root.exists():
        raise WikiAlreadyExists(name, wiki.data_root)
    for root in (
        wiki.data_root,
        wiki.config_root,
        wiki.cache_root,
        wiki.state_root,
        wiki.runtime_root,
    ):
        root.mkdir(parents=True, exist_ok=True)
    WikiLayout(wiki.data_root).init()
    copy_default_schema(wiki.schema_path)
    git_init_initial(wiki.data_root)
    return {
        "name": wiki.name,
        "data_root": str(wiki.data_root),
        "config_root": str(wiki.config_root),
        "cache_root": str(wiki.cache_root),
        "state_root": str(wiki.state_root),
        "runtime_root": str(wiki.runtime_root),
        "version": __version__,
    }


# ---------------------------------------------------------------------------
# ingest_source — atomic ingest through the orchestrator
# ---------------------------------------------------------------------------


@mcp.tool
def ingest_source(source: str, name: str | None = None) -> str:
    """Ingest ``source`` into the wiki identified by ``name``.

    Delegates to ``Orchestrator.run_ingest``.
    """
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki=wiki)
    return orch.run_ingest(source)


# ---------------------------------------------------------------------------
# wiki_search / wiki_read — direct memory retrieval
# ---------------------------------------------------------------------------


@mcp.tool
def wiki_search(
    question: str,
    collection_ids: list[str] | None = None,
    limit: int = 5,
    name: str | None = None,
) -> dict[str, object]:
    """Search the wiki identified by ``name`` for project knowledge."""
    from lies.memory.service import WikiMemoryService

    wiki = resolve_wiki(name)
    result = WikiMemoryService(wiki).search(
        question,
        collection_ids=collection_ids,
        limit=limit,
    )
    return cast(dict[str, object], result.model_dump())


@mcp.tool
def wiki_read(
    page_ids: list[str],
    name: str | None = None,
) -> dict[str, str]:
    """Read full wiki pages by ID for the wiki identified by ``name``."""
    from lies.memory.service import WikiMemoryService

    wiki = resolve_wiki(name)
    return WikiMemoryService(wiki).read(page_ids)


# ---------------------------------------------------------------------------
# query — extractive answer with structured fallback metadata
# ---------------------------------------------------------------------------


@mcp.tool
def query(question: str, name: str | None = None) -> SynthesizedMcpAnswer:
    """Answer ``question`` from the wiki identified by ``name``.

    Uses the qmd → index.md fallback path. The result always has
    ``fallback_used`` and ``fallback_reason`` populated so the caller
    can distinguish a real qmd-backed answer from a fallback.
    """
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki=wiki)
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
def lint(name: str | None = None, fix: bool = False) -> str:
    """Run lint; with fix=True also apply the repair plan."""
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki=wiki)
    return orch.run_lint(apply=fix)


# ---------------------------------------------------------------------------
# Resources — raw wiki reads, no LLM round-trip
# ---------------------------------------------------------------------------
#
# Each resource is defined as an ``_impl`` function (takes ``name``,
# does the real work) plus a thin zero-argument forwarder that FastMCP
# registers as the static-resource handler. The forwarder pattern is a
# FastMCP 3.4.5 constraint: static-resource handlers must be
# zero-argument. Keeping the impl beside the forwarder lets tests and
# direct callers exercise the real logic with an explicit ``name``.


def _wiki_status_impl(name: str | None = None) -> str:
    """Return qmd status plus the last 10 lines of ``wiki/log.md``.

    If qmd is unavailable, the error is embedded in the returned string
    (resource reads must never fail loudly for a degraded-but-functional
    state).
    """
    wiki = resolve_wiki(name)
    out = "=== qmd status ===\n"
    try:
        from lies.qmd import qmd_status as _qmd_status

        out += _qmd_status(wiki.data_root)
    except Exception as exc:  # noqa: BLE001 - degraded path must surface, not crash
        out += f"qmd unavailable: {exc}"
    out += "\n\n=== last 10 log entries ===\n"
    log_path = wiki.wiki_dir / "log.md"
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").splitlines()
        for line in lines[-10:]:
            out += line + "\n"
    else:
        out += "(no log yet)\n"
    return out


@mcp.resource("wiki://status")
def wiki_status() -> str:
    """qmd status + last 10 log lines.

    Zero-argument forwarder (FastMCP 3.4.5 constraint on static-resource
    handlers). Real logic in :func:`_wiki_status_impl`; ``name`` is
    resolved from the env there.
    """
    return _wiki_status_impl()


def _wiki_index_impl(name: str | None = None) -> str:
    wiki = resolve_wiki(name)
    index_path = wiki.wiki_dir / "index.md"
    if not index_path.exists():
        return ""
    return index_path.read_text(encoding="utf-8")


@mcp.resource("wiki://index")
def wiki_index() -> str:
    """Raw contents of ``wiki/index.md`` (empty string if absent).

    Zero-argument forwarder (FastMCP 3.4.5 constraint). Real logic in
    :func:`_wiki_index_impl`; ``name`` is resolved from the env there.
    """
    return _wiki_index_impl()


def _wiki_log_impl(name: str | None = None) -> str:
    wiki = resolve_wiki(name)
    log_path = wiki.wiki_dir / "log.md"
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8")


@mcp.resource("wiki://log")
def wiki_log() -> str:
    """Raw contents of ``wiki/log.md`` (empty string if absent).

    Zero-argument forwarder (FastMCP 3.4.5 constraint). Real logic in
    :func:`_wiki_log_impl`; ``name`` is resolved from the env there.
    """
    return _wiki_log_impl()


def _wiki_lint_report_impl(name: str | None = None) -> str:
    wiki = resolve_wiki(name)
    report_path = wiki.wiki_dir / "lint-report.md"
    if not report_path.exists():
        return ""
    return report_path.read_text(encoding="utf-8")


@mcp.resource("wiki://lint-report")
def wiki_lint_report() -> str:
    """Raw contents of ``wiki/lint-report.md`` (empty string if absent).

    Zero-argument forwarder (FastMCP 3.4.5 constraint). Real logic in
    :func:`_wiki_lint_report_impl`; ``name`` is resolved from the env
    there.
    """
    return _wiki_lint_report_impl()


def _safe_page_path(wiki: Wiki, path: str) -> Path:
    """Resolve ``path`` under ``wiki.wiki_dir`` and reject escapes.

    ``path`` is wiki-dir-relative. Absolute paths, ``..`` traversal,
    and any path that resolves outside ``wiki.wiki_dir`` are rejected
    with :class:`WikiPlanInvalid`. Missing files are not an error at
    this layer — callers decide what to do with the returned path.
    """
    if not path:
        raise WikiPlanInvalid("page path is empty")
    candidate = Path(path)
    if candidate.is_absolute():
        raise WikiPlanInvalid(f"page path must be relative: {path}")
    if any(part == ".." for part in candidate.parts):
        raise WikiPlanInvalid(f"page path contains '..': {path}")
    resolved = (wiki.wiki_dir / candidate).resolve()
    try:
        resolved.relative_to(wiki.wiki_dir.resolve())
    except ValueError as exc:
        raise WikiPlanInvalid(f"page path escapes wiki/: {path}") from exc
    return resolved


def _wiki_page_impl(path: str, name: str | None = None) -> str:
    """Return the raw markdown of any page under ``wiki/``.

    ``path`` is relative to ``<wiki.data_root>/wiki/``. Absolute paths,
    ``..`` traversal, and any path that resolves outside the wiki are
    rejected with :class:`WikiPlanInvalid`. Missing files return ``""``
    (the resource exists; the page just hasn't been written yet).
    """
    wiki = resolve_wiki(name)
    resolved = _safe_page_path(wiki, path)
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
