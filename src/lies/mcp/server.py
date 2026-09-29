"""FastMCP server exposing the LIES v0.40 tool surface.

The v0.40 rewrite retires the legacy wiki-shaped read tools
(``wiki_search`` / ``wiki_read`` / ``wiki_catalog`` /
``file_knowledge`` / ``query`` / ``answer``), the wiki-shaped
read tools the F19 grounding archivist shipped (``mcp_ground``
/ ``mcp_synthesize``), and the seven starter-template MCP
prompts (``answer`` / ``orient`` / ``ingest`` / ``lint`` /
``sync`` / ``file-back`` / ``cite``).

The replacement read surface is four stateless MCP tools that
map onto the v0.40 design contract:

- ``collections_read`` — live library registry reader.
- ``search`` — single-batch hybrid vec+lex qmd query.
- ``read`` — verbatim page bodies via source-aware dispatch.
- ``lib_ask`` — librarian + synthesizer orchestrator.

``lint`` and ``reindex`` stay on the surface as operational /
diagnostic primitives.

Spec:
``~/code/project-notes/lies/superpowers/specs/2026-09-26-librarian-v040-port-design.md``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, cast

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

try:
    from fastmcp import Context
except ImportError:  # FastMCP < 3.4.5 with Context.elicit
    Context: type | None = None  # type: ignore[assignment,misc]

from lies.lock_errors import WikiFlockUnrepairable, WikiLockBusy
from lies.mcp.collections import collections_read as _collections_read
from lies.mcp.instructions_loader import load_instructions
from lies.mcp.prompts import register_prompts
from lies.mcp.read import read as _read_tool
from lies.mcp.resolution import resolve_wiki
from lies.mcp.search import search as _search_tool
from lies.mcp.synth import lib_ask as _lib_ask_tool
from lies.orchestrator import Orchestrator
from lies.query.tag_expr import TagExprUnknown

mcp = FastMCP(
    "lies",
    instructions=load_instructions(),
)


# Bind slash-command prompts BEFORE tool registrations so prompts are
# queryable before tools when the wire first boots. See
# ``lies.mcp.prompts.register_prompts``.
register_prompts(mcp)


# ---------------------------------------------------------------------------
# Tag-filter helpers (shared with CLI + grounding archivist)
# ---------------------------------------------------------------------------
#
# ``format_unknown_tag_error`` is imported by ``lies.cli.query`` for
# the CLI's same-shape error message; ``_collect_available_tags_mcp``
# is imported by ``lies.mcp.grounding`` for the F15 validator. Both
# stay here as library-side helpers — neither is an MCP tool — until
# the CLI/grounding callers import them from a more neutral location.


def _collect_available_tags_mcp(wiki: object) -> set[str]:
    """Return every addressable tag in the library (MCP surface).

    Thin shim over :func:`lies.library.registry.library_collection_names`
    — kept so the MCP-side validator has the same helper name as its
    CLI counterpart. ``wiki`` is accepted for signature uniformity
    with the legacy per-wiki resolution but is intentionally ignored:
    collections live in the library, not in any wiki.

    Each collection name is added both bare and with the ``c:``
    qualifier prefix so the F15 tag-expression validator recognizes
    ``c:<name>`` atoms as addressable on the MCP ``query`` / ``answer``
    path. Each ``LibraryCollectionConfig.tags`` entry is added both
    bare and with the ``t:`` qualifier prefix so ``t:<tag>`` filters
    against a library-collection tag do not raise ``TagExprUnknown`` —
    and so bare ``+tag`` expressions validate too, since F15 treats a
    bare atom as the implicit-t alias for ``+t:tag``. Both lookups
    are wrapped in ``try/except`` so an uninitialized library — or
    any other registry failure — does not break the validator; the
    function still returns a set, just one that does not include
    library tags of the failed surface.
    """
    from lies.library.registry import library_collection_names, library_collection_tags

    try:
        names = library_collection_names()
    except Exception:
        names = frozenset()
    tags: set[str] = set(names) | {f"c:{name}" for name in names}
    try:
        for tag in library_collection_tags():
            tags.add(tag)
            tags.add(f"t:{tag}")
    except Exception:
        pass
    return tags


def format_unknown_tag_error(exc: TagExprUnknown) -> str:
    """Build a self-explanatory ``unknown tag`` ToolError message.

    Library-first surface. Up to three lines:

      - offending tag spelling (``'opencode'``)
      - the library's collections sorted (``the library's collections: ...``),
        when the library is initialized with at least one collection
      - when the library is empty / absent, a distinct line that
        tells the operator whether to initialize the library or to
        ingest something into it (two separate failure modes).

    The library is NOT a wiki and is never referred to as one. The
    original ``unknown tag: <tag>`` prefix is preserved so log
    scrapers and existing tests that grep on the literal string keep
    working.
    """
    from lies.library.registry import (
        library_has_no_collections,
        library_initialized,
    )

    parts: list[str] = [f"unknown tag: {exc.tag!r}"]
    if exc.available:
        sorted_tags = sorted(exc.available)
        parts.append(f"the library's collections: {', '.join(sorted_tags)}")
    elif not library_initialized():
        parts.append(
            "the library is not initialized; collections live in the library, "
            "not in wikis. Initialize it before querying with tag filters."
        )
    elif library_has_no_collections():
        parts.append(
            "the library has no collections; ingest something first "
            "(see `lies ingest --help`) before querying with tag filters."
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# v0.40 read-side tool registrations
# ---------------------------------------------------------------------------
#
# The four new tools are imported from their dedicated modules and
# registered under their canonical v0.40 names. ``Tool.from_function``
# already wraps each function in a FastMCP ``FunctionTool``; we pass
# ``search.fn`` / ``read.fn`` / ``lib_ask.fn`` to ``mcp.tool(name=...)``
# (which expects a bare callable) so the FastMCP wire re-wraps the
# underlying function rather than trying to wrap a Tool object. The
# explicit ``name=`` kwarg matches the canonical MCP tool name;
# ``Tool.from_function`` already encoded it but the redeclaration
# is the documentation surface for readers scanning the wire shape.

mcp.tool(name="collections_read")(_collections_read)
mcp.tool(name="search")(_search_tool.fn)
mcp.tool(name="read")(_read_tool.fn)
mcp.tool(name="lib_ask")(_lib_ask_tool.fn)


# ---------------------------------------------------------------------------
# Operational diagnostics: lint + reindex (unchanged)
# ---------------------------------------------------------------------------


class _ConfirmDestructive(BaseModel):
    """Schema for the destructive-flag elicit prompt.

    Two-field payload (``confirm``, optional ``reason``) used to
    record the operator's intent before destructive operations
    (``reindex --cleanup``, ``reindex --all``).
    """

    model_config = ConfigDict(frozen=True)

    confirm: bool
    reason: str = ""


async def _confirm_destructive(ctx: Context | None, message: str) -> str | None:  # type: ignore[valid-type]
    """Prompt the user; return ``None`` to proceed or an error string to abort.

    Hosts that don't implement ``ctx.elicit`` raise on the call; we
    return a clear error string so the caller treats it as decline (no
    work runs). ``ctx`` may be ``None`` for programmatic callers; the
    ``try/except`` below catches the resulting ``AttributeError`` and
    surfaces the same "elicitation unavailable" error path the existing
    callers rely on.
    """
    try:
        result: Any = await ctx.elicit(  # ty: ignore[unresolved-attribute]
            message,
            response_type=cast(Any, _ConfirmDestructive),
        )
    except Exception as exc:  # pragma: no cover — host-dependent
        return f"elicitation unavailable: {exc}"

    if result.action != "accept":
        return "operation declined by user"
    if result.data is None or not result.data.confirm:
        return "operation declined by user"
    return None


@mcp.tool(
    description=(
        "Reindex QMD collections. --cleanup/--all are destructive and "
        "elicit confirmation. Returns ReindexResult with reconciled/"
        "indexed/embedded/cleaned flags and errors list."
    ),
    annotations=ToolAnnotations(
        title="Reindex: rebuild QMD index",
        destructive_hint=True,
    ),
)
async def reindex(
    cleanup: bool = False,
    all_: bool = False,
    embed: bool = False,
    force: bool = False,
    reconcile: bool = False,
    name: str | None = None,
    ctx: Context | None = None,  # type: ignore[valid-type]
) -> dict[str, object]:
    """Rebuild qmd index; gate destructive flags.

    Mirrors ``lies reindex`` CLI. Destructive flags (``cleanup`` /
    ``all_``) elicit confirmation via ``_confirm_destructive``. Decline
    or elicit unavailable → no work runs, error returned in the
    ``ReindexResult`` envelope. ``ToolAnnotations(destructive_hint=True)``
    signals the destructive nature to host UIs even when the user hasn't
    passed the destructive flags yet (the gate runs before qmd).
    """
    from lies.etl.sync_helper import collection_names, sync_collection
    from lies.qmd import _models
    from lies.qmd.cli import qmd_reindex

    wiki = resolve_wiki(name)

    result = _models.ReindexResult()

    # Optional pre-step: sync each collection before reindex so the
    # rebuild sees fresh raw mirrors. Mirrors ``lies reindex --reconcile``.
    if reconcile:
        for coll_name in collection_names(wiki, None):
            sync_collection(wiki, coll_name, force=False)
        result.reconciled = True

    # Gate destructive flags. When ``ctx`` is None (programmatic caller)
    # ``_confirm_destructive`` raises on ``ctx.elicit`` and the helper
    # returns ``elicitation unavailable``; we treat that as decline so
    # no work runs (safety preserved through the helper's exception
    # path, symmetric with the CLI's refuse-on-non-TTY-without-yes).
    if cleanup or all_:
        if all_:
            prompt = "Reindex --all will run cleanup + reindex + embed (full rebuild). Confirm?"
        else:
            prompt = "Cleanup will vacuum the FTS5 db and drop orphan rows. Confirm?"
        decision = await _confirm_destructive(ctx, prompt)
        if decision is not None:
            # Detect elicitation-unavailable specifically so the LLM
            # caller gets a bypass path rather than a generic
            # protocol-version error string. Bug E (session f39c9ef8):
            # the host's MCP connection was older than
            # ``2026-07-28`` and rejected server-initiated elicitation;
            # ``_confirm_destructive`` returned
            # ``"elicitation unavailable: <inner-exc>"`` and the LLM
            # had no actionable signal to route on. Surface the
            # concrete workaround here so the caller can either
            # restart the MCP daemon (which negotiates a newer
            # protocol version) or invoke ``lies reindex --cleanup``
            # directly from the shell, where destructive flags run
            # without the MCP gate.
            if decision.startswith("elicitation unavailable"):
                bypass_msg = (
                    "cleanup requires confirmation; MCP server-initiated "
                    "elicitation unavailable on this connection. Run "
                    "`lies mcp down && lies mcp up` and retry, or invoke "
                    "`lies reindex --cleanup` directly from the shell."
                )
                return _models.ReindexResult(
                    reconciled=result.reconciled, errors=[bypass_msg]
                ).model_dump()
            return _models.ReindexResult(
                reconciled=result.reconciled, errors=[decision]
            ).model_dump()

    reindex_outcome = qmd_reindex(
        wiki.wiki_dir,
        embed=embed,
        cleanup=cleanup,
        all_=all_,
        force=force,
    )
    result.indexed = reindex_outcome.indexed
    result.embedded = reindex_outcome.embedded
    result.cleaned = reindex_outcome.cleaned
    result.errors = reindex_outcome.errors
    return result.model_dump()


@mcp.tool
def lint(
    name: str | None = None,
    fix: bool = False,
    force_repair: bool = False,
) -> str:
    """Run lint; with ``fix=True`` also apply the repair plan.

    When ``fix=True`` and ``force_repair=True``, the cross-process
    memory flock is unconditionally reaped + retried once before
    surfacing ``WikiFlockUnrepairable`` if a live contender still
    holds it; without the flag, a live contender raises
    ``WikiLockBusy``. Flock errors are caught and returned as an
    ``error:``-prefixed string so the MCP server stays up; other
    exceptions propagate to the MCP error path.
    """
    wiki = resolve_wiki(name)
    orch = Orchestrator(wiki=wiki)
    try:
        return orch.run_lint(apply=fix, force_repair=force_repair)
    except (WikiFlockUnrepairable, WikiLockBusy):
        return f"error: {sys.exc_info()[1]}"


# ---------------------------------------------------------------------------
# Resources — wiki operational diagnostics + library catalog
# ---------------------------------------------------------------------------
#
# Each resource is defined as an ``_impl`` function (takes ``name``,
# does the real work) plus a thin zero-argument forwarder that FastMCP
# registers as the static-resource handler. The forwarder pattern is a
# FastMCP 3.4.5 constraint: static-resource handlers must be
# zero-argument. Keeping the impl beside the forwarder lets tests and
# direct callers exercise the real logic with an explicit ``name``.


def _register_static_resource(
    uri: str, fn: Callable[..., str], description: str | None = None
) -> Callable[..., str]:
    """Register ``fn`` as a *static* MCP resource even when it has params.

    FastMCP's ``@mcp.resource(...)`` decorator auto-routes any callable
    with parameters to :class:`ResourceTemplate`, which then rejects
    URIs without ``{...}`` placeholders. The wiki resource surface
    accepts a wiki ``name`` kwarg (resolving through
    :func:`lies.mcp.resolution.resolve_wiki`); the resource surface
    needs the same parity for direct Python callers and tests, so we
    bypass the decorator's auto-routing by registering a
    :class:`FunctionResource` directly. FastMCP's read path still
    invokes the registered ``fn`` with no positional arguments; any
    ``name``-style kwargs are passed only by Python callers (tests,
    REPL, direct imports), not by the MCP wire.
    """
    from fastmcp.resources.function_resource import FunctionResource

    resource = FunctionResource.from_function(
        fn=fn,
        uri=uri,
        description=description,
    )
    mcp.add_resource(resource)
    return fn


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


def _wiki_status_mcp() -> str:
    """Zero-arg FastMCP handler for ``wiki://status``."""
    return _wiki_status_impl()


_register_static_resource(
    "wiki://status",
    _wiki_status_mcp,
    description="qmd status + last 10 log lines.",
)


def wiki_status(name: str | None = None) -> str:
    """qmd status + last 10 log lines."""
    return _wiki_status_impl(name)


def _wiki_index_impl(name: str | None = None) -> str:
    """Raw ``wiki/index.md`` contents (JSON envelope in library mode).

    Wiki mode returns the raw markdown of ``wiki/index.md``; the
    empty-file case returns ``""``. Library mode returns a stable
    envelope ``{"mode": "library"}``.
    """
    import json

    try:
        wiki = resolve_wiki(name)
    except Exception:
        return json.dumps({"mode": "library"}, indent=2)

    if not wiki.data_root.exists():
        return json.dumps({"mode": "library"}, indent=2)

    index_path = wiki.wiki_dir / "index.md"
    if not index_path.exists():
        return ""
    return index_path.read_text(encoding="utf-8")


def _wiki_index_mcp() -> str:
    """Zero-arg FastMCP handler for ``wiki://index``."""
    return _wiki_index_impl()


_register_static_resource(
    "wiki://index",
    _wiki_index_mcp,
    description="Raw contents of wiki/index.md.",
)


def wiki_index(name: str | None = None) -> str:
    """Raw contents of ``wiki/index.md`` (JSON envelope in library mode)."""
    return _wiki_index_impl(name)


def _wiki_log_impl(name: str | None = None) -> str:
    wiki = resolve_wiki(name)
    log_path = wiki.wiki_dir / "log.md"
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8")


def _wiki_log_mcp() -> str:
    """Zero-arg FastMCP handler for ``wiki://log``."""
    return _wiki_log_impl()


_register_static_resource(
    "wiki://log",
    _wiki_log_mcp,
    description="Raw contents of wiki/log.md.",
)


def wiki_log(name: str | None = None) -> str:
    """Raw contents of ``wiki/log.md`` (empty string if absent)."""
    return _wiki_log_impl(name)


def _wiki_lint_report_impl(name: str | None = None) -> str:
    """Raw ``wiki/lint-report.md`` contents (JSON envelope in library mode)."""
    import json

    try:
        wiki = resolve_wiki(name)
    except Exception:
        return json.dumps({"mode": "library", "status": "no_wiki"}, indent=2)

    if not wiki.data_root.exists():
        return json.dumps({"mode": "library", "status": "no_wiki"}, indent=2)

    report_path = wiki.wiki_dir / "lint-report.md"
    if not report_path.exists():
        return ""
    return report_path.read_text(encoding="utf-8")


def _wiki_lint_report_mcp() -> str:
    """Zero-arg FastMCP handler for ``wiki://lint-report``."""
    return _wiki_lint_report_impl()


_register_static_resource(
    "wiki://lint-report",
    _wiki_lint_report_mcp,
    description="Raw contents of wiki/lint-report.md.",
)


def wiki_lint_report(name: str | None = None) -> str:
    """Raw contents of ``wiki/lint-report.md`` (JSON envelope in library mode)."""
    return _wiki_lint_report_impl(name)


# ---------------------------------------------------------------------------
# library://catalog — read-through for the library collection registry
# ---------------------------------------------------------------------------


def _count_pages(name: str) -> int:
    """Count ``*.md`` files under a library collection's doc tree.

    Each collection is rooted at ``<library>/collections/<name>/`` and
    carries its markdown under ``doc/`` (post-ingest). We walk the
    whole subtree for ``*.md`` files; empty / missing trees return 0.
    """
    from lies.library.paths import Library

    root = Library.open().collections_root / name / "doc"
    if not root.exists():
        return 0
    return sum(1 for _ in root.rglob("*.md"))


def _library_collection_payload(slug: str) -> dict | None:
    """Build the per-collection metadata envelope for ``slug``.

    Returns ``None`` when ``slug`` is not a registered collection.
    """
    from lies.library.config_io import load_config
    from lies.library.errors import CollectionNotFound

    try:
        cfg = load_config(slug)
    except CollectionNotFound:
        return None
    return {
        "name": cfg.name,
        "tags": list(cfg.tags),
        "source": cfg.source,
        "page_count": _count_pages(slug),
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
    }


@mcp.resource("library://catalog")
def library_catalog() -> str:
    """All library collection metadata as a per-collection grouping.

    Returns JSON of shape::

        {
          "<collection-name>": {
            "name": "<collection-name>",
            "tags": ["..."],
            "source": "<source-url>",
            "page_count": <int>,
            "updated_at": "<iso-8601>"
          },
          ...
        }

    Collections without a ``config.yaml`` (not yet bootstrapped) are
    silently skipped — the resource only surfaces collections the
    library knows about through its registry. Empty library → ``{}``.
    """
    import json

    from lies.library.registry import library_collection_names

    out: dict[str, dict] = {}
    for name in sorted(library_collection_names()):
        payload = _library_collection_payload(name)
        if payload is None:
            continue
        out[name] = payload
    return json.dumps(out, indent=2)


@mcp.resource("library://catalog/{slug}")
def library_catalog_slug(slug: str) -> str:
    """Single library collection metadata; empty string when not found.

    Returns ``""`` (not a JSON object) when ``slug`` is not a
    registered collection so an LLM caller can distinguish "missing"
    from "present with empty fields" cheaply. Empty-string is the
    same contract the retired ``wiki://catalog/{slug}`` resource
    used — clients that pattern-matched on it keep working.
    """
    import json

    payload = _library_collection_payload(slug)
    if payload is None:
        return ""
    return json.dumps(payload, indent=2)
