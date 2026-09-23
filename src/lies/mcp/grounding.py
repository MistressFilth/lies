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

import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
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
    """

    question: str
    tag_expr: str | None
    exclude_expr: "TagExpr | None"
    citations: list[CitationSnippet]
    no_coverage: bool
    distinct_pages: int


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


def ground(
    question: str,
    tag_expr: str | None = None,
    exclude_expr: "TagExpr | None" = None,
    top_k: int = 3,
    *,
    wiki_name: str | None = None,
) -> ArchivistDigest:
    """Return a grounding digest for ``question``.

    Translates ``tag_expr`` / ``exclude_expr`` via the F15 tag-filter
    dispatch, calls the F18 librarian, and trims each excerpt to a
    ≤200-char grounding snippet. The caller renders the result as
    ``[[slug]]: "snippet"`` per ask's grounding form (NOT F19's long
    ``[[slug]]: "verbatim"`` form).

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

    Returns:
        :class:`ArchivistDigest` carrying the librarian's excerpts
        trimmed to ≤200 chars each.

    Raises:
        ArchivistCoverageError: when a positive tag matches zero
            collections (caller may retry untagged or surface). Also
            raised when the include expression fails to parse.
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
    if tag_expr is not None:
        # Lazy imports keep this module off the pydantic_ai / fastmcp
        # import path that ``utils.logging`` is also careful to avoid.
        from lies.library.registry import library_collection_names
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
            # The registry's lru_cache is populated with a ``frozenset``
            # so the resolver's ``available=set(...)`` materialization
            # does not mutate the cache. ``sorted`` re-orders for the
            # deterministic error-message envelope below — the cache
            # itself is already an ordered frozenset so re-sorting is
            # redundant on the happy path but cheap (small N) and keeps
            # the error message stable against future cache-shape
            # changes.
            resolve(include_ast, available=set(library_collection_names()))
        except TagExprUnknown as exc:
            # ``library_collection_names`` already returns a sorted
            # frozenset (see ``lies.library.registry``), so the
            # ``sorted(...)`` here is a no-op-on-shape defense against
            # future cache-shape changes — and ``exc.available`` (the
            # resolver's set of every known atom) is unsorted by
            # contract, so we sort it for the deterministic error
            # envelope below.
            available = (
                sorted(exc.available) if exc.available else sorted(library_collection_names())
            )
            raise ArchivistCoverageError(
                f"unknown tag(s): {exc.tag!r} (available: {available!r})"
            ) from exc

    # Lazy imports — ``LibrarianDeps`` transitively pulls in
    # ``pydantic_ai`` and the orchestrator's tool registry. Keeping
    # the import inside ``ground`` mirrors the CLI's lazy-import
    # pattern (see ``tests/unit/cli/test_cli_lazy_imports``).
    from lies.agents.librarian import (
        LibrarianDeps,
        LibrarianOutput,
        register_librarian_tools,
    )

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

        agent = librarian_agent()
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
            # API credentials). Build a no-tools agent so the
            # dispatch-exception branch can still catch any
            # downstream failure; the digest lands as
            # ``no_coverage=True`` / ``citations=[]``.
            try:
                agent = librarian_agent(model="test")
            except Exception:
                # No model available at all — give up gracefully.
                return ArchivistDigest(
                    question=question,
                    tag_expr=resolved_tag_expr,
                    exclude_expr=exclude_expr,
                    citations=[],
                    no_coverage=True,
                    distinct_pages=0,
                )

    deps = LibrarianDeps(
        question=question,
        tag_expr=resolved_tag_expr,
        exclude_expr=exclude_expr,
        top_k=top_k,
    )

    try:
        librarian_result = agent.run_sync(question, deps=deps)
        out: LibrarianOutput = librarian_result.output
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
    )
