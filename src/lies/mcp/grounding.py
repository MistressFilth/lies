"""Grounding archivist — `ground()` Python function + helpers.

Wraps the F18 librarian to return a tight grounding digest: up to
``top_k`` CitationSnippet entries (≤200 chars each), keyed by bare slug
for ``[[slug]]: "snippet"`` rendering. Library vs wiki discrimination
lives on ``CitationSnippet.collection``. No LLM call in this module.

The read path goes through ``lies.qmd.access.daemon_tool`` so the
fan-out uses the daemon's ``query`` tool with the full ``collections``
push-down (one call replaces a per-collection loop).
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from lies.agents.librarian import PageExcerpt
    from lies.markdown_spans import Span
    from lies.query.tag_expr import TagExpr


from lies.agents.librarian import librarian_agent  # noqa: E402,F401


_log = logging.getLogger(__name__)


def _current_timeout() -> int:
    from lies.config import get_qmd_query_timeout

    return get_qmd_query_timeout()


@dataclass(frozen=True)
class CitationSnippet:
    """One grounding snippet — ≤200 chars from the first prose span of a page.

    Attributes:
        collection: ``"wiki"`` or library-collection-name.
        slug: ``<collection>/<page>`` form (e.g. ``claude_code/hooks.md``),
            not the bare ``page`` segment. The full path is what the
            citation contract renders as ``[[slug]]: "snippet"`` and
            is what an agent needs to disambiguate hits across
            collections with the same page name (``setup.md`` in two
            collections is two distinct documents).
        title: Human-readable page title.
        snippet: First ≤200 chars of the first prose span.
        source_kind: ``"library"`` (primary) or ``"wiki"`` (wiki-only,
            not grounded in a primary source). Library-wins-on-conflict
            drops the wiki copy on slug match.
    """

    collection: str
    slug: str
    title: str
    snippet: str
    source_kind: Literal["library", "wiki"] = "library"


@dataclass(frozen=True)
class ArchivistDigest:
    """The grounding archivist's return value.

    Attributes:
        question: Echoed back for caller verification.
        tag_expr: Chosen union (``None`` when untagged).
        exclude_expr: Compiled NOT AST (``None`` when no ``-`` chain).
        citations: Snippets, one per retrieved excerpt.
        no_coverage: True on a scope miss (librarian's
            ``LibrarianOutput.no_coverage``) or a clean zero-hits
            result. A *process* failure sets ``transient`` instead.
        transient: True on a dispatch failure (process-side,
            not corpus-side). The two flags are independent:
            ``no_coverage=False, transient=True`` is a slow / failed
            retrieval, not "the corpus has nothing".
        distinct_pages: ``len({c.slug for c in citations})``.
        searched_scope: Sorted, unique collection names actually
            dispatched. The daemon serves an unknown collection with
            an empty result and no error, so a scope that names
            something the daemon does not have is reported here as
            what was sent, not as what was asked for.
        unserved_scope: Requested collection names the daemon does
            not serve, in input order. Disjoint from
            ``searched_scope``. A digest with a non-empty
            ``unserved_scope`` and an empty ``searched_scope``
            searched nothing, which is not a statement about the
            corpus.
        no_library: True when the library is uninitialized.
    """

    question: str
    tag_expr: str | None
    exclude_expr: "TagExpr | None"
    citations: list[CitationSnippet]
    no_coverage: bool
    distinct_pages: int
    searched_scope: list[str] = field(default_factory=list)
    unserved_scope: list[str] = field(default_factory=list)
    no_library: bool = False
    transient: bool = False


class ArchivistCoverageError(Exception):
    """Tagged query hit zero collections (unknown tag)."""


def truncate_at_word_boundary(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ≤ ``max_chars``, cutting at the last whitespace.

    ``str.isspace()`` covers space, tab, newline, etc. — valid word
    boundaries in prose. If no whitespace, hard-cut at ``max_chars``.

    Raises:
        ValueError: if ``max_chars < 1``.
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be ≥ 1, got {max_chars}")
    if len(text) <= max_chars:
        return text
    candidate = text[:max_chars]
    last_ws = -1
    for i, c in enumerate(candidate):
        if c.isspace():
            last_ws = i
    if last_ws >= 0:
        return candidate[:last_ws].rstrip()
    return candidate


def pick_first_prose_span(spans: "list[Span]") -> "Span | None":
    """Return the first prose span from ``spans``, or ``None``.

    Skips code-fence spans and empty bodies.
    """
    for span in spans:
        if span.code_fence:
            continue
        if not span.body.strip():
            continue
        return span
    return None


# ---------------------------------------------------------------------------
# top-level orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FanoutResult:
    """What a single-batch fan-out actually did.

    ``searched`` and ``unserved`` partition the requested collection
    names. Returning them alongside the excerpts is what keeps
    ``searched_scope`` a statement about the dispatch rather than
    about the request: the daemon answers an unknown collection with
    an empty result and no error, so a fan-out that silently drops
    an unserved name reports the scope it *asked for* and lets a
    caller read a scope problem as "the corpus has nothing".
    """

    excerpts: "list[PageExcerpt]"
    searched: list[str]
    unserved: list[str]


async def _fanout_collections(
    question: str,
    exclude_expr: "TagExpr | None",
    top_k: int,
    collection_names: list[str],
) -> _FanoutResult:
    """One daemon ``query`` against the resolved collection set, no LLM round-trip.

    The daemon's ``collections`` parameter is a true push-down: the
    candidate set is narrowed inside qmd, so a single hybrid call
    against the resolved list returns in-scope rows from every named
    collection. ``exclude_expr`` is preserved for signature parity
    but not enforced here (per-collection qmd filters are include-only).

    Pre-validates ``collection_names`` against the daemon's binding
    collection set via :func:`access.validate_scope` so one
    unresolvable name inside the batched array cannot silently
    surface as a clean miss. The shared helper is the same one
    ``search`` uses, so the two cannot drift.

    When *every* requested name is unserved the result is a fan-out
    that searched nothing, and says so: ``searched`` is empty and
    ``unserved`` names the collections. The caller must not read that
    as a clean miss.

    Raises:
        QmdDaemonUnavailable: the daemon is not serving (operator action).
        QmdDaemonWedged: the daemon stopped answering; ``last_output``
            carries the tail of the daemon's own log.
    """
    from lies.agents.librarian import PageExcerpt
    from lies.markdown_spans import Span
    from lies.qmd import access

    del exclude_expr

    if not collection_names:
        return _FanoutResult(excerpts=[], searched=[], unserved=[])

    validated, unserved = await access.validate_scope(collection_names)
    if unserved:
        _log.warning(
            "ground: daemon does not serve %d of %d requested collections: %s",
            len(unserved),
            len(collection_names),
            ", ".join(unserved),
        )
    if not validated:
        return _FanoutResult(excerpts=[], searched=[], unserved=unserved)

    searched = sorted(set(validated))
    searches = [
        {"type": "lex", "query": question},
        {"type": "vec", "query": question},
    ]
    arguments: dict[str, object] = {
        "searches": searches,
        "limit": top_k,
        "collections": searched,
        "intent": "lies.mcp.grounding fan-out (single-batch hybrid)",
    }

    result = await access.daemon_tool("query", arguments, timeout=float(_current_timeout()))

    structured = getattr(result, "structured_content", None) or {}
    rows = structured.get("results") or []
    if not isinstance(rows, list):
        rows = []

    # Defence in depth: the daemon honours ``limit`` today; the slice
    # stops a future backend that ignores the wire argument.
    rows = rows[:top_k]

    out: list[PageExcerpt] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        file_value = row.get("file")
        if not isinstance(file_value, str) or not file_value:
            continue
        path = file_value
        coll = path.split("/", 1)[0] if path else ""
        slug = path
        title = row.get("title") or path.rsplit("/", 1)[-1].replace(".md", "")
        snippet = row.get("snippet")
        spans: list[Span] = []
        if snippet:
            spans = [
                Span(
                    heading_path=[],
                    body=str(snippet),
                    code_fence=False,
                    start_line=int(row.get("line", 1)),
                ),
            ]
        out.append(
            PageExcerpt(
                collection=coll,
                slug=slug,
                title=title,
                spans=spans,
                source_kind="library",
            )
        )
    return _FanoutResult(excerpts=out, searched=searched, unserved=unserved)


async def _fanout_unscoped(
    question: str,
    exclude_expr: "TagExpr | None",
    top_k: int,
) -> _FanoutResult:
    """Daemon ``query`` against every registered library collection, unscoped.

    Bypasses the F18 librarian LLM round-trip by issuing a single
    daemon ``query`` with the collections push-down.
    """
    from lies.library.registry import library_collection_metas

    names = sorted(m.name for m in library_collection_metas())
    return await _fanout_collections(question, exclude_expr, top_k, names)


async def _query_tagged_collections(
    question: str,
    exclude_expr: "TagExpr | None",
    top_k: int,
    collection_names: list[str],
) -> _FanoutResult:
    """Daemon ``query`` against the tag-resolved collection set.

    Fast-path replacing the F18 librarian round-trip on tagged queries.
    """
    return await _fanout_collections(question, exclude_expr, top_k, collection_names)


async def ground(
    question: str,
    tag_expr: str | None = None,
    exclude_expr: "TagExpr | None" = None,
    top_k: int = 3,
    *,
    wiki_name: str | None = None,
    librarian_model: "Model | str | None" = None,
) -> ArchivistDigest:
    """Return a grounding digest for ``question``.

    Async because the unscoped and tagged fast-paths bridge to async
    fan-out helpers via ``await``; the legacy F18 librarian path is the
    only sync branch.

    Translates ``tag_expr`` / ``exclude_expr`` via the F15 tag-filter
    dispatch, then dispatches one of three retrieval paths:

    1. **Unscoped fast-path** (no tag/exclude) — direct qmd fan-out
       across every registered library collection.
    2. **Tagged fast-path** — when the resolved AST matches ≥1 library
       collection, direct qmd fan-out across ONLY the matched ones.
    3. **Legacy F18 librarian** — when the tagged AST matches zero
       library collections.

    Args:
        question: Natural-language question to ground.
        tag_expr: Body of a single include expression (no leading
            sigil), e.g. ``"airflow&postgres"``. ``None`` for untagged.
        exclude_expr: Compiled NOT AST. ``None`` when no ``-`` chain.
        top_k: Maximum excerpts (clamped to ``[1, 10]``).
        wiki_name: Optional wiki name; defaults to env-default.
        librarian_model: Pre-resolved ``Model`` or model-string for
            the F18 librarian. Consumed only on the legacy path.

    Returns:
        :class:`ArchivistDigest` carrying the librarian's excerpts
        trimmed to ≤200 chars each.

    Raises:
        ArchivistCoverageError: positive tag matches zero collections
            or include expression fails to parse.
        lies.errors.ModelNotConfigured: legacy librarian path entered
            without a resolved model.
    """
    if top_k < 1:
        top_k = 1
    elif top_k > 10:
        top_k = 10

    resolved_tag_expr = tag_expr
    include_ast: "TagExpr | None" = None
    if tag_expr is not None:
        from lies.mcp.server import _collect_available_tags_mcp
        from lies.query.tag_expr import (
            TagExprEmpty,
            TagExprParseError,
            TagExprUnknown,
            parse,
            resolve,
        )

        try:
            include_ast = parse(tag_expr)
        except (TagExprParseError, TagExprEmpty) as exc:
            raise ArchivistCoverageError(f"invalid tag expression: {exc}") from exc
        try:
            # Use the same expanded available-set as the MCP ``query`` /
            # ``answer`` boundary so bare-tag includes (``+claude``) and
            # explicit ``t:`` / ``c:`` forms validate consistently.
            resolve(
                include_ast,
                available=_collect_available_tags_mcp(wiki=None),
            )
        except TagExprUnknown as exc:
            available = (
                sorted(exc.available)
                if exc.available
                else sorted(_collect_available_tags_mcp(wiki=None))
            )
            raise ArchivistCoverageError(
                f"unknown tag(s): {exc.tag!r} (available: {available!r})"
            ) from exc

    # F15 ``searched_scope`` (Bug C fix): mirror ``Orchestrator.run_query``'s
    # envelope. Computed BEFORE the librarian dispatch so every return
    # path carries the same scope.
    from lies.query.synthesizer import (
        _all_collection_names,
        _collections_matching,
    )
    from lies.query.tag_expr import ResolvedTagFilter

    if include_ast is None and exclude_expr is None:
        searched_scope_list: list[str] = _all_collection_names()
    else:
        resolved_for_scope = ResolvedTagFilter(
            include=include_ast,
            exclude=exclude_expr,
        )
        try:
            searched_scope_list = sorted(_collections_matching(resolved_for_scope))
        except Exception:
            searched_scope_list = []

    if not searched_scope_list and tag_expr is None and exclude_expr is None:
        return ArchivistDigest(
            question=question,
            tag_expr=None,
            exclude_expr=None,
            citations=[],
            no_coverage=True,
            distinct_pages=0,
            searched_scope=[],
            no_library=True,
        )

    from lies.agents.librarian import (
        LibrarianDeps,
        LibrarianOutput,
        register_librarian_tools,
    )
    from lies.qmd import access

    out: LibrarianOutput
    fanout: _FanoutResult | None = None
    if tag_expr is None and exclude_expr is None:
        # Unscoped fast-path: bypass the F18 librarian LLM round-trip.
        try:
            fanout = await _fanout_unscoped(question, exclude_expr, top_k)
        except access.QmdDaemonUnavailable:
            raise
        except access.QmdDaemonWedged as exc:
            raise access.QmdDaemonWedged(
                f"ground: qmd daemon wedged during fan-out; last qmd output: {exc.last_output!r}"
                if exc.last_output
                else "ground: qmd daemon wedged during fan-out",
                last_output=exc.last_output,
            ) from exc
        except Exception as exc:
            # A dispatch failure is a process claim, not a corpus
            # claim. ``transient=True`` distinguishes "the daemon
            # failed" from "the corpus has nothing", and the logger
            # line keeps a persistently failing daemon visible
            # (``warnings.warn`` filters once per location by default).
            _log.error(
                "ground: fan-out dispatch failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return ArchivistDigest(
                question=question,
                tag_expr=resolved_tag_expr,
                exclude_expr=exclude_expr,
                citations=[],
                no_coverage=False,
                distinct_pages=0,
                searched_scope=searched_scope_list,
                no_library=False,
                transient=True,
            )
        out = LibrarianOutput(
            tag_expr=None,
            exclude_expr=None,
            excerpts=fanout.excerpts,
            distinct_pages=len({e.slug for e in fanout.excerpts}),
            no_coverage=len(fanout.excerpts) == 0,
        )
    elif (tag_expr is not None or exclude_expr is not None) and searched_scope_list:
        # Tagged fast-path: bypass F18 when AST matches ≥1 collection.
        try:
            fanout = await _query_tagged_collections(
                question,
                exclude_expr,
                top_k,
                searched_scope_list,
            )
        except access.QmdDaemonUnavailable:
            raise
        except access.QmdDaemonWedged as exc:
            raise access.QmdDaemonWedged(
                f"ground: qmd daemon wedged during tagged fan-out; "
                f"last qmd output: {exc.last_output!r}"
                if exc.last_output
                else "ground: qmd daemon wedged during tagged fan-out",
                last_output=exc.last_output,
            ) from exc
        except Exception as exc:
            _log.error(
                "ground: tagged fan-out dispatch failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return ArchivistDigest(
                question=question,
                tag_expr=resolved_tag_expr,
                exclude_expr=exclude_expr,
                citations=[],
                no_coverage=False,
                distinct_pages=0,
                searched_scope=searched_scope_list,
                no_library=False,
                transient=True,
            )
        out = LibrarianOutput(
            tag_expr=resolved_tag_expr,
            exclude_expr=exclude_expr,
            excerpts=fanout.excerpts,
            distinct_pages=len({e.slug for e in fanout.excerpts}),
            no_coverage=len(fanout.excerpts) == 0,
        )
    else:
        # Legacy F18 librarian path: build the agent, wire its tools,
        # then ``run_sync``. Tool wiring is best-effort; the dispatch-
        # exception branch returns ``no_coverage=True`` on any tool-side
        # failure.
        agent = None
        try:
            from lies.mcp.resolution import resolve_wiki
            from lies.memory.service import WikiMemoryService

            agent = librarian_agent(model=librarian_model)
            resolved_wiki = resolve_wiki(wiki_name)
            register_librarian_tools(
                agent,
                wiki=resolved_wiki,
                memory_service=WikiMemoryService(resolved_wiki),
            )
        except Exception as wiring_exc:
            warnings.warn(
                f"ground: tool wiring skipped (bare agent will dispatch): "
                f"{type(wiring_exc).__name__}: {wiring_exc}",
                stacklevel=2,
            )
            if agent is None:
                raise

        deps = LibrarianDeps(
            question=question,
            tag_expr=resolved_tag_expr,
            exclude_expr=exclude_expr,
            top_k=top_k,
        )

        try:
            librarian_result = agent.run_sync(question, deps=deps)
            out = librarian_result.output
        except Exception as exc:
            _log.error(
                "ground: librarian dispatch failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return ArchivistDigest(
                question=question,
                tag_expr=resolved_tag_expr,
                exclude_expr=exclude_expr,
                citations=[],
                no_coverage=False,
                distinct_pages=0,
                searched_scope=searched_scope_list,
                no_library=False,
                transient=True,
            )

    citations: list[CitationSnippet] = []
    for excerpt in out.excerpts:
        spans = list(excerpt.spans)
        chosen = pick_first_prose_span(spans)
        if chosen is None:
            continue
        snippet_text = truncate_at_word_boundary(chosen.body.strip(), 200)
        if not snippet_text:
            continue
        source_kind = getattr(excerpt, "source_kind", "library")
        citations.append(
            CitationSnippet(
                collection=excerpt.collection,
                slug=excerpt.slug,
                title=excerpt.title,
                snippet=snippet_text,
                source_kind=source_kind,
            )
        )

    # A dispatch that searched nothing because the daemon serves none
    # of the requested collections is not a clean miss. It reports an
    # empty scope and names what it could not get; the caller decides
    # what to tell the user, and nothing here claims the corpus is
    # empty.
    if fanout is not None and not fanout.searched:
        return ArchivistDigest(
            question=question,
            tag_expr=resolved_tag_expr,
            exclude_expr=exclude_expr,
            citations=[],
            no_coverage=False,
            distinct_pages=0,
            searched_scope=[],
            unserved_scope=fanout.unserved,
            no_library=False,
        )

    searched = fanout.searched if fanout is not None else searched_scope_list
    unserved = fanout.unserved if fanout is not None else []

    return ArchivistDigest(
        question=question,
        tag_expr=resolved_tag_expr,
        exclude_expr=exclude_expr,
        citations=citations,
        no_coverage=out.no_coverage,
        distinct_pages=len({c.slug for c in citations}),
        searched_scope=searched,
        unserved_scope=unserved,
        no_library=False,
    )
