"""Grounding archivist — `ground()` Python function + helpers.

Wraps the F18 librarian (shipped at v0.33.0) to return a tight
grounding digest: up to ``top_k`` CitationSnippet entries (≤200
chars each), keyed by bare slug for ``[[slug]]: "snippet"``
rendering. Reuses the F37 span parser and F15 tag-filter dispatch.
``ArchivistDigest.no_coverage`` flows directly from the F18
``LibrarianOutput.no_coverage`` bundle field — the catalog probe
that previously fed it has been retired in favor of the librarian's
own scope-miss signal.

The MCP tool wrapper around :func:`ground` ships in Task 3. This
file holds the library function plus the :class:`CitationSnippet`,
:class:`ArchivistDigest`, :class:`ArchivistCoverageError`, and
span-picking helpers. Library vs wiki discrimination lives on
``CitationSnippet.collection``. No LLM call in this module.
"""

from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from lies.agents.librarian import PageExcerpt
    from lies.markdown_spans import Span
    from lies.query.tag_expr import TagExpr


# Module-level import of the librarian factory so callers (and tests)
# can monkeypatch ``grounding.librarian_agent``. The factory itself is
# cheap; the pydantic_ai cost only pays when ``.run_sync(...)`` fires.
from lies.agents.librarian import librarian_agent  # noqa: E402,F401


@dataclass(frozen=True)
class CitationSnippet:
    """One grounding snippet — ≤200 chars from the first prose span of a page.

    Attributes:
        collection: ``"wiki"`` or library-collection-name (e.g.
            ``"claude_platform"``).
        slug: Bare slug for ``[[slug]]: "snippet"`` rendering. Examples:
            ``"concepts/pydantic"``, ``"entities/postgres"``.
        title: Human-readable page title.
        snippet: First ≤200 chars of the first prose span of the page.
        source_kind: Where the snippet came from. ``"library"`` =
            primary source (library collection); ``"wiki"`` =
            wiki-only hit (secondary, not grounded in a primary
            source). Library-wins-on-conflict drops the wiki copy
            on slug match, so the merged row never carries the
            wiki discriminator. Defaults to ``"library"`` for
            backward compat.
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
        exclude_expr: Compiled NOT AST the caller passed through
            (Task 3 / f15-exclude-compound). ``None`` when no
            ``-`` chain was supplied. The historical
            ``exclude_tags: list[str]`` contract was retired along
            with ``ResolvedTagFilter.exclude`` so the AST threads
            through to the librarian unchanged.
        citations: Snippets, one per retrieved excerpt.
        no_coverage: True when the F18 librarian's bundle reports a
            scope miss on a populated wiki (corpus non-empty AND no
            hits landed). Source-of-truth is the librarian's
            ``LibrarianOutput.no_coverage`` field as of F18 Task 2.
        distinct_pages: ``len({c.slug for c in citations})``.
        searched_scope: Sorted, unique list of library collection
            names whose corpus was searched. Mirrors
            ``SynthesizedAnswer.searched_scope`` (Bundle C / F15):
            every collection in the library when untagged, or the
            sorted set of collections whose ``atom_matches`` is true
            for the resolved include / exclude AST when tagged.
            Empty when the library is uninitialized or the resolved
            AST matches no collections. Populated even on the
            ``no_coverage=True`` path so callers can render
            "searched X, found nothing" rather than guessing.
    """

    question: str
    tag_expr: str | None
    exclude_expr: "TagExpr | None"
    citations: list[CitationSnippet]
    no_coverage: bool
    distinct_pages: int
    searched_scope: list[str] = field(default_factory=list)
    no_library: bool = False


class ArchivistCoverageError(Exception):
    """Tagged query hit zero collections (unknown tag)."""


def truncate_at_word_boundary(text: str, max_chars: int) -> str:
    """Truncate ``text`` to ≤ ``max_chars``, cutting at the last whitespace.

    Cuts at the last whitespace class character (space, tab, newline,
    or any other ``str.isspace()`` char) at or before ``max_chars``.
    This matters because :func:`lies.markdown_spans.parse_spans` joins
    multi-line prose bodies with ``"\\n"`` — a single-line ASCII-space
    rule would hard-cut mid-word across paragraph seams. No trailing
    partial word; no trailing whitespace. If ``text`` has no
    whitespace at all, hard-cut at ``max_chars``. ``max_chars``
    must be ≥ 1.

    Args:
        text: The string to truncate.
        max_chars: Maximum length of the result (must be ≥ 1).

    Returns:
        Truncated string of length ≤ ``max_chars``.

    Raises:
        ValueError: if ``max_chars < 1``.
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be ≥ 1, got {max_chars}")
    if len(text) <= max_chars:
        return text
    # Look for the last whitespace-class character at or before position
    # max_chars. ``str.isspace()`` covers space, tab, newline, CR, FF,
    # and VT — all of which are valid word boundaries in prose.
    candidate = text[:max_chars]
    last_ws = -1
    for i, c in enumerate(candidate):
        if c.isspace():
            last_ws = i
    if last_ws >= 0:
        return candidate[:last_ws].rstrip()
    # No whitespace in the candidate — hard cut at max_chars.
    return candidate


def pick_first_prose_span(spans: "list[Span]") -> "Span | None":
    """Return the first prose span from ``spans``, or ``None``.

    Skips code-fence spans (``code_fence=True``) and empty bodies
    (``body.strip() == ""``). The synthesis's pipeline already
    separates prose from code, so this filter is the F19 ground
    shape's lens on F37 spans.
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


async def _fanout_collections(
    question: str,
    exclude_expr: "TagExpr | None",
    top_k: int,
    collection_names: list[str],
) -> "list[PageExcerpt]":
    """Parallel-scan ``collection_names`` for the question, no LLM round-trip.

    Shared core for both the unscoped fan-out
    (:func:`_fanout_unscoped`, Task 1) and the tagged fan-out
    (:func:`_query_tagged_collections`, this task). Bypasses the F18
    librarian entirely by dispatching per-collection ``qmd_query``
    calls concurrently and merging the results sorted by score desc.
    Each ``collection_names`` entry is passed as a qmd
    ``collection_filter`` set so the per-collection post-filter
    retains the same semantics the unscoped fan-out has shipped with.

    Per-collection timeout: 5s. Failed collections dropped silently.
    The ``exclude_expr`` parameter is preserved for signature parity
    with the broader ground() surface; per-collection qmd filters are
    include-only, so excludes are not enforced at this layer (the
    include filter already constrains the addressable set).

    Args:
        question: Natural-language question.
        exclude_expr: Compiled NOT AST (Task 3 / f15-exclude-compound).
            ``None`` when no ``-`` chain supplied. Retained for
            signature parity; not enforced inside the helper.
        top_k: Maximum excerpts to return.
        collection_names: Library collection names to scan. The
            caller is responsible for resolving the set — this helper
            does not consult the library registry. Order is
            irrelevant; duplicates are de-duped by sorting.

    Returns:
        ``list[PageExcerpt]`` sorted by score desc, length ≤ ``top_k``.
        Empty list when ``collection_names`` is empty or all fan-outs
        fail.
    """
    from lies.agents.librarian import PageExcerpt
    from lies.library.registry import library_git_root
    from lies.markdown_spans import Span
    from lies.qmd.cli import QmdCommandError, QmdNoResultsError, qmd_query

    if not collection_names:
        return []

    lib_root = library_git_root()
    names = sorted(set(collection_names))

    async def _one(name: str) -> list[dict] | None:
        try:
            return await asyncio.to_thread(
                qmd_query,
                cwd=lib_root,
                question=question,
                limit=top_k,
                timeout=5,
                collection_filter={name},
            )
        except (QmdCommandError, QmdNoResultsError):
            return None

    raw = await asyncio.gather(*[_one(n) for n in names])
    merged: list[tuple[float, dict]] = []
    for batch in raw:
        if not batch:
            continue
        for hit in batch:
            score = float(hit.get("score", 0.0))
            merged.append((score, hit))
    merged.sort(key=lambda x: x[0], reverse=True)
    top_hits = merged[:top_k]

    out: list[PageExcerpt] = []
    for score, hit in top_hits:
        path = hit.get("path", "")
        # path is "<collection>/<rest>"; collection name is the first segment
        coll = path.split("/", 1)[0] if path else ""
        slug = path
        title = hit.get("title") or path.rsplit("/", 1)[-1].replace(".md", "")
        # qmd's wire payload carries a ``snippet`` field with a brief
        # diff-style excerpt (``@@ -N,M @@ (before, after) ...``) — use
        # it to seed a single prose span so the downstream citation-
        # building loop in :func:`ground` can produce a
        # :class:`CitationSnippet`. Without a span, the loop skips
        # the excerpt (no prose body to truncate), producing an empty
        # citations list — pre-this-change the fan-out path always
        # returned 0 citations in production despite the qmd scan
        # surfacing real hits. Falls back to an empty spans list when
        # qmd omits the field (older qmd CLI versions, edge-case
        # qmd-shape changes).
        snippet = hit.get("snippet")
        spans: list[Span] = []
        if snippet:
            spans = [
                Span(
                    heading_path=[],
                    body=str(snippet),
                    code_fence=False,
                    start_line=int(hit.get("line", 1)),
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
    return out


async def _fanout_unscoped(
    question: str,
    exclude_expr: "TagExpr | None",
    top_k: int,
) -> "list[PageExcerpt]":
    """Parallel-scan every registered library collection for unscoped queries.

    Thin wrapper around :func:`_fanout_collections` that resolves
    the collection set from the library registry. Bypasses the F18
    librarian LLM round-trip (which times out at ~42s/empty on
    unscoped queries — session 2505630b reproduction) by fanning
    out directly to qmd with a per-collection post-filter. Returns
    merged ``PageExcerpt`` rows sorted by score desc and truncated
    to ``top_k``.

    Per-collection timeout: 5s. Failed collections dropped silently.

    Args:
        question: Natural-language question.
        exclude_expr: Compiled NOT AST (Task 3 / f15-exclude-compound).
            ``None`` when no ``-`` chain supplied.
        top_k: Maximum excerpts to return.

    Returns:
        ``list[PageExcerpt]`` sorted by score desc, length ≤ ``top_k``.
        Empty list when no collections registered or all fan-outs fail.
    """
    from lies.library.registry import library_collection_metas

    names = sorted(m.name for m in library_collection_metas())
    return await _fanout_collections(question, exclude_expr, top_k, names)


async def _query_tagged_collections(
    question: str,
    exclude_expr: "TagExpr | None",
    top_k: int,
    collection_names: list[str],
) -> "list[PageExcerpt]":
    """Parallel-scan the tag-resolved collection set without the F18 librarian.

    Tagged ``ground()`` previously took the F18 librarian path even
    when the resolved AST matched one or more library collections
    (session 2505630b reproduction — ``ground(tag_expr="c:switchyard")``
    timed out at ~197s with ``no_coverage=True``). This fast-path
    replaces the librarian LLM round-trip with a direct qmd fan-out
    across ONLY the tag-matched collections (the unscoped fast-path
    fanned across every registered collection).

    Mirrors the F18 ``LibrarianOutput.excerpts`` shape so the
    downstream citation-building loop in :func:`ground` is identical
    between the unscoped and the tagged fast-path. The library is
    the universe for retrieval; the wiki surface contributes only
    via the legacy F18 librarian fallback when the resolved AST
    matches zero collections.

    Args:
        question: Natural-language question.
        exclude_expr: Compiled NOT AST (Task 3 / f15-exclude-compound).
            ``None`` when no ``-`` chain supplied.
        top_k: Maximum excerpts to return.
        collection_names: Sorted collection names from the F15
            ``_collections_matching`` walker — the addressable set
            that the tagged query resolves to.

    Returns:
        ``list[PageExcerpt]`` sorted by score desc, length ≤ ``top_k``.
        Empty list when ``collection_names`` is empty or all fan-outs
        fail.
    """
    return await _fanout_collections(question, exclude_expr, top_k, collection_names)


def ground(
    question: str,
    tag_expr: str | None = None,
    exclude_expr: "TagExpr | None" = None,
    top_k: int = 3,
    *,
    wiki_name: str | None = None,
    librarian_model: "Model | str | None" = None,
) -> ArchivistDigest:
    """Return a grounding digest for ``question``.

    Translates ``tag_expr`` / ``exclude_expr`` via the F15 tag-filter
    dispatch, then dispatches one of three retrieval paths:

    1. **Unscoped fast-path** (no ``tag_expr``, no ``exclude_expr``) —
       direct qmd fan-out across every registered library collection
       (Task 1 brick-wall fix; ~1s vs the historical ~42s librarian
       round-trip on an empty wiki).
    2. **Tagged fast-path** (this task) — when ``tag_expr`` or
       ``exclude_expr`` is set AND the resolved AST matches at least
       one library collection, direct qmd fan-out across ONLY the
       matched collections. Replaces the F18 librarian round-trip
       that previously timed out at ~197s on
       ``tag_expr="c:switchyard"`` (session 2505630b reproduction).
    3. **Legacy F18 librarian** — when the tagged AST matches zero
       library collections (edge-case fallback that preserves
       historical behavior for ``tag_expr="c:ghost"`` style queries
       against an unpopulated library).

    Trims each excerpt to a ≤200-char grounding snippet. The caller
    renders the result as ``[[slug]]: "snippet"`` per ask's
    grounding form (NOT F19's long ``[[slug]]: "verbatim"`` form).

    Args:
        question: The natural-language question to ground.
        tag_expr: Body of a single include expression (no leading
            sigil), e.g. ``"airflow&postgres"``. ``None`` for untagged.
        exclude_expr: Compiled NOT AST (Task 3 / f15-exclude-compound).
            ``None`` when no ``-`` chain was supplied. The historical
            ``exclude_tags: list[str]`` parameter was retired in
            Task 3 along with ``ResolvedTagFilter.exclude``; callers
            build the AST via the CLI / MCP parser and thread it
            through here unchanged.
        top_k: Maximum excerpts requested from the librarian (clamped
            to ``[1, 10]``). The librarian honors the request;
            ``ground`` does not re-truncate its output.
        wiki_name: Optional wiki name to resolve against. Defaults to
            the env-default (``LIES_WIKI_NAME`` or ``"default"``).
            Tests pass an explicit name so the dispatch layer hits
            a known fixture wiki; production callers leave it
            ``None``.
        librarian_model: Pre-resolved ``Model`` or model-string for
            the F18 librarian. ``None`` falls through to the bare
            :func:`librarian_agent` factory, which raises
            :class:`ModelNotConfigured` (the historical
            pre-resolver behavior). The MCP ``ground`` tool wrapper
            resolves this via :func:`lies.mcp.server._resolve_librarian_model`
            so the configuration error surfaces at the MCP boundary
            rather than mid-dispatch. Only consumed on the legacy
            librarian path (path #3 above); both fast-paths bypass
            the LLM round-trip entirely.

    Returns:
        :class:`ArchivistDigest` carrying the librarian's excerpts
        trimmed to ≤200 chars each. The digest's ``searched_scope``
        mirrors ``Orchestrator.run_query``'s envelope: every
        registered library collection when untagged, or the sorted
        set of collections whose ``atom_matches`` is true for the
        resolved include / exclude AST when tagged. Populated even on
        the ``no_coverage=True`` path so callers can render
        "searched X, found nothing" rather than guessing.

    Raises:
        ArchivistCoverageError: when a positive tag matches zero
            collections (caller may retry untagged or surface). Also
            raised when the include expression fails to parse.
        lies.errors.ModelNotConfigured: when ``librarian_model`` is
            ``None`` and the legacy librarian path (#3 above) is
            entered (empty library OR zero tag matches). The MCP
            wrapper pre-resolves the model so this propagates only
            when a Python caller skips the resolver.
    """
    if top_k < 1:
        top_k = 1
    elif top_k > 10:
        top_k = 10

    # F15 tag-filter dispatch: parse + validate include. Excludes are
    # passed through to the librarian unchanged (the librarian owns
    # exclude-side enforcement per Bundle C). ``resolve`` raises
    # ``TagExprUnknown`` when an include atom is not in the addressable
    # collection set — that IS "positive tag matches zero collections"
    # at the dispatch layer.
    resolved_tag_expr = tag_expr
    include_ast: "TagExpr | None" = None
    if tag_expr is not None:
        # Lazy imports keep this module off the pydantic_ai / fastmcp
        # import path that ``utils.logging`` is also careful to avoid.
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
            # explicit ``t:`` / ``c:`` forms validate consistently across
            # every MCP tool that maps to the F15 grammar. The set
            # covers bare collection names, ``c:<name>`` atoms, AND
            # both bare and ``t:<tag>`` library-collection tag entries
            # — without that, ``+claude`` (which parses to
            # ``Include("claude", qualifier=None)``) raised
            # ``TagExprUnknown`` against the collection-names-only set
            # the helper used pre-v0.37.9.
            resolve(
                include_ast,
                available=_collect_available_tags_mcp(wiki=None),
            )
        except TagExprUnknown as exc:
            # ``_collect_available_tags_mcp`` already returns a
            # deterministic-shape set (sorted frozenset for the
            # collection side; bare / ``t:`` / ``c:`` aliases for the
            # tag side), so the ``sorted(...)`` here is a no-op-on-
            # shape defense against future cache-shape changes — and
            # ``exc.available`` (the resolver's set of every known
            # atom) is unsorted by contract, so we sort it for the
            # deterministic error envelope below. The fallback when
            # ``exc.available`` is empty reads from the same expanded
            # helper so the surfaced ``available:`` list matches the
            # validator's view end-to-end.
            available = (
                sorted(exc.available)
                if exc.available
                else sorted(_collect_available_tags_mcp(wiki=None))
            )
            raise ArchivistCoverageError(
                f"unknown tag(s): {exc.tag!r} (available: {available!r})"
            ) from exc

    # F15 ``searched_scope`` (Bug C fix): mirror the contract that
    # ``Orchestrator.run_query`` writes onto
    # ``SynthesizedAnswer.searched_scope``. Source is the library
    # registry — the library is the universe; wikis do not contribute
    # to the addressable collection set. Untagged -> every registered
    # collection, sorted. Tagged -> the sorted set of collections
    # whose ``atom_matches`` is true for the resolved include /
    # exclude AST. Empty when the library is uninitialized or the
    # resolved AST matches zero collections.
    #
    # Computed BEFORE the librarian dispatch so every return path —
    # no model available, librarian exception, success — carries the
    # same scope envelope. The orchestrator does the same:
    # ``searched_scope`` lands on the answer before the F18
    # ``no_coverage`` decision, so a ``no_coverage=True`` answer still
    # tells the operator which collections the system tried.
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
            # ``_collections_matching`` walks ``library_collection_metas``
            # against the resolved AST. If the registry lookup raises
            # (e.g. mid-write corruption), degrade to an empty list
            # rather than failing the digest — same fail-soft posture
            # the orchestrator's ``except Exception: answer.searched_scope = []``
            # branch takes on the same line.
            searched_scope_list = []

    # Task 1 brick-wall fix: unscoped ``ground()`` previously took the
    # F18 librarian path, hitting a ~42s LLM round-trip and returning
    # 0 citations (session 2505630b reproduction). Replace that path
    # with a direct qmd fan-out across registered library collections
    # when the query is unscoped (no tag include, no exclude AST) AND
    # the library has at least one collection. The ``no_library=True``
    # fast-path below handles the empty-library case up front so the
    # MCP surface can distinguish "library uninitialized" from
    # "library initialized but no hits".
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

    # Lazy imports — ``LibrarianDeps`` transitively pulls in
    # ``pydantic_ai`` and the orchestrator's tool registry. Keeping
    # the import inside ``ground`` mirrors the CLI's lazy-import
    # pattern (see ``tests/unit/cli/test_cli_lazy_imports``).
    from lies.agents.librarian import (
        LibrarianDeps,
        LibrarianOutput,
        register_librarian_tools,
    )

    out: LibrarianOutput
    if tag_expr is None and exclude_expr is None:
        # Unscoped fast-path: bypass the F18 librarian LLM round-trip
        # and fan out directly to qmd across every registered library
        # collection. ``asyncio.run`` bridges the sync ``ground()``
        # signature to the async fan-out helper; the surface stays
        # synchronous for every existing caller. Skips the librarian
        # tool-wiring block entirely (no LLM round-trip happens here).
        try:
            excerpts = asyncio.run(
                _fanout_unscoped(question, exclude_expr, top_k),
            )
        except Exception as exc:
            warnings.warn(
                f"ground: fan-out dispatch failed: {type(exc).__name__}: {exc}",
                stacklevel=2,
            )
            return ArchivistDigest(
                question=question,
                tag_expr=resolved_tag_expr,
                exclude_expr=exclude_expr,
                citations=[],
                no_coverage=True,
                distinct_pages=0,
                searched_scope=searched_scope_list,
                no_library=False,
            )
        out = LibrarianOutput(
            tag_expr=None,
            exclude_expr=None,
            excerpts=excerpts,
            distinct_pages=len({e.slug for e in excerpts}),
            no_coverage=len(excerpts) == 0,
        )
    elif (tag_expr is not None or exclude_expr is not None) and searched_scope_list:
        # Tagged fast-path: when the resolved AST matches at least one
        # library collection, bypass the F18 librarian LLM round-trip
        # (session 2505630b reproduction: ``ground(tag_expr="c:switchyard")``
        # previously timed out at ~197s with ``no_coverage=True`` and 0
        # citations) and fan out directly to qmd across ONLY the matched
        # collections. Same async-to-sync bridge as the unscoped path;
        # same ``LibrarianOutput`` shape downstream so the citation-
        # building loop is identical between the two fast-paths.
        #
        # Gate: ``tag_expr is not None or exclude_expr is not None`` keeps
        # the unscoped path exclusive (this branch is for tagged queries
        # only), and ``searched_scope_list`` is the resolved collection
        # set — non-empty iff the library has collections AND the AST
        # matches at least one. The library is the universe for
        # retrieval; when the AST matches zero collections we fall
        # through to the legacy F18 librarian path (edge-case fallback
        # preserves historical behavior for ``tag_expr`` set with no
        # library match — e.g. ``tag_expr="c:ghost"``).
        try:
            excerpts = asyncio.run(
                _query_tagged_collections(
                    question,
                    exclude_expr,
                    top_k,
                    searched_scope_list,
                ),
            )
        except Exception as exc:
            warnings.warn(
                f"ground: tagged fan-out dispatch failed: {type(exc).__name__}: {exc}",
                stacklevel=2,
            )
            return ArchivistDigest(
                question=question,
                tag_expr=resolved_tag_expr,
                exclude_expr=exclude_expr,
                citations=[],
                no_coverage=True,
                distinct_pages=0,
                searched_scope=searched_scope_list,
                no_library=False,
            )
        out = LibrarianOutput(
            tag_expr=resolved_tag_expr,
            exclude_expr=exclude_expr,
            excerpts=excerpts,
            distinct_pages=len({e.slug for e in excerpts}),
            no_coverage=len(excerpts) == 0,
        )
    else:
        # Construct the librarian agent and wire its tools BEFORE run_sync.
        # The bare factory emits ``Agent(tools=[])`` so an unwired agent
        # has no tools; the F18 4-step contract (classify → search → read
        # → return) cannot run, and the LLM either emits an empty digest
        # or attempts the named tools and crashes — the dispatch-exception
        # branch below would then return ``no_coverage=True``. Wiring the
        # active wiki's ``WikiMemoryService`` lets the librarian's
        # ``wiki_search`` / ``wiki_read`` / ``wiki_catalog`` closures see
        # the per-wiki context.
        #
        # Tool wiring is best-effort: when the active wiki cannot be
        # resolved (no wiki registered, XDG misconfigured) OR the agent
        # factory itself raises (e.g. missing API credentials), the agent
        # falls back to a no-tools bare-agent path and the dispatch-
        # exception branch handles any tool-side failure. The user-
        # visible signal flows through stdlib ``warnings`` so a non-
        # configured logfire environment does not emit
        # ``LogfireNotConfiguredWarning`` noise.
        agent = None
        try:
            from lies.mcp.resolution import resolve_wiki
            from lies.memory.service import WikiMemoryService

            # ``librarian_agent()`` raises ``ModelNotConfigured`` when
            # called without a ``model=`` kwarg — see the factory doc.
            # The MCP wrapper resolves the model eagerly via
            # ``_resolve_librarian_model`` and threads it through this
            # kwarg; Python callers (e.g. ``lies query --ground``) can
            # pre-resolve and pass the same shape, or let it raise.
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
                # Agent construction itself failed (most likely missing
                # API credentials). Re-raise so the caller sees the
                # underlying ModelNotConfigured; the digest contract
                # never promised a no-tools fallback.
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
            # The codebase's dominant warning surface for runtime
            # anomalies is stdlib ``warnings`` (see e.g.
            # ``src/lies/wiki_settings.py``), not logfire. logfire is
            # reserved for instrumentation in :func:`utils.logging.configure_logging`.
            # ``logfire.warning`` from a non-configured environment
            # emits ``LogfireNotConfiguredWarning`` on every call, which
            # is noise in tests and CLI runs without a LOGFIRE_TOKEN.
            # Switch to ``warnings.warn`` so the user-visible signal
            # stays out of logfire's wiring entirely.
            warnings.warn(
                f"ground: librarian dispatch failed: {type(exc).__name__}: {exc}",
                stacklevel=2,
            )
            return ArchivistDigest(
                question=question,
                tag_expr=resolved_tag_expr,
                exclude_expr=exclude_expr,
                citations=[],
                no_coverage=True,
                distinct_pages=0,
                searched_scope=searched_scope_list,
                no_library=False,
            )

    citations: list[CitationSnippet] = []
    for excerpt in out.excerpts:
        # ``PageExcerpt.spans`` is ``list[Span]`` at v0.33.0 — the
        # canonical surface that F37's span parser produced. No legacy
        # blob shape survives in the current librarian output; a
        # defensive ``parse_spans(str(excerpt.spans))`` fallback would
        # only ever produce garbage input. Read the attribute
        # directly.
        spans = list(excerpt.spans)
        chosen = pick_first_prose_span(spans)
        if chosen is None:
            continue
        snippet_text = truncate_at_word_boundary(chosen.body.strip(), 200)
        if not snippet_text:
            continue
        # ``source_kind`` (dual-source-routing) — read from each
        # ``PageExcerpt`` so the citation preserves the
        # library-vs-wiki provenance the librarian tagged. Default
        # to ``"library"`` for any excerpt that doesn't yet carry
        # the field (forward-compat against pre-T2F fixtures).
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

    # ``no_coverage`` per the F18 Task 2 contract: the value flows
    # directly from the librarian's ``LibrarianOutput.no_coverage``
    # bundle field. The librarian is the source of truth for the
    # scope-miss signal — the catalog probe that previously fed this
    # branch has been retired in favor of the F18 bundle field.
    no_coverage = out.no_coverage

    return ArchivistDigest(
        question=question,
        tag_expr=resolved_tag_expr,
        exclude_expr=exclude_expr,
        citations=citations,
        no_coverage=no_coverage,
        distinct_pages=len({c.slug for c in citations}),
        searched_scope=searched_scope_list,
        no_library=False,
    )
