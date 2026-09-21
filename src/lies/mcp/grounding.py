"""Grounding archivist — `ground()` Python function + helpers.

Wraps the F18 librarian (shipped at v0.33.0) to return a tight
grounding digest: up to ``top_k`` CitationSnippet entries (≤200
chars each), keyed by bare slug for ``[[slug]]: "snippet"``
rendering. Reuses the F37 span parser and F15 tag-filter dispatch.

The MCP tool wrapper around :func:`ground` ships in Task 3. This
file holds the library function plus the :class:`CitationSnippet`,
:class:`ArchivistDigest`, :class:`ArchivistCoverageError`, and
span-picking helpers. Library vs wiki discrimination lives on
``CitationSnippet.collection``. No LLM call in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lies.markdown_spans import parse_spans

if TYPE_CHECKING:
    from lies.markdown_spans import Span


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
    """

    collection: str
    slug: str
    title: str
    snippet: str


@dataclass(frozen=True)
class ArchivistDigest:
    """The grounding archivist's return value.

    Attributes:
        question: Echoed back for caller verification.
        tag_expr: Chosen union (``None`` when untagged).
        exclude_tags: NOT tags the caller passed through.
        citations: Snippets, one per retrieved excerpt.
        no_coverage: True when the corpus is non-empty AND no hits
            landed (scope miss on a populated wiki).
        distinct_pages: ``len({c.slug for c in citations})``.
    """

    question: str
    tag_expr: str | None
    exclude_tags: list[str]
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
    exclude_tags: "list[str] | None" = None,
    top_k: int = 3,
) -> ArchivistDigest:
    """Return a grounding digest for ``question``.

    Translates ``tag_expr`` / ``exclude_tags`` via the F15 tag-filter
    dispatch, calls the F18 librarian, and trims each excerpt to a
    ≤200-char grounding snippet. The caller renders the result as
    ``[[slug]]: "snippet"`` per ask's grounding form (NOT F19's long
    ``[[slug]]: "verbatim"`` form).

    Args:
        question: The natural-language question to ground.
        tag_expr: Body of a single include expression (no leading
            sigil), e.g. ``"airflow&postgres"``. ``None`` for untagged.
        exclude_tags: NOT tags without leading sigil. The F15 grammar
            permits at most one; passing more is forwarded to the
            librarian unchanged.
        top_k: Maximum excerpts requested from the librarian (clamped
            to ``[1, 10]``). The librarian honors the request;
            ``ground`` does not re-truncate its output.

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
    exclude_list: list[str] = list(exclude_tags or [])

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
            resolve(include_ast, available=set(library_collection_names()))
        except TagExprUnknown as exc:
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
    )

    deps = LibrarianDeps(
        question=question,
        tag_expr=resolved_tag_expr,
        exclude_tags=exclude_list,
        top_k=top_k,
    )

    try:
        librarian_result = librarian_agent().run_sync(question, deps=deps)
        out: LibrarianOutput = librarian_result.output
    except Exception as exc:
        # Lazy import — ``logfire`` is not on the bare CLI import path
        # (``utils.logging`` does the same dance). We only need the
        # logger when the librarian dispatch fails, so paying the
        # import cost in the except branch is acceptable.
        import logfire

        logfire.warning("ground: librarian dispatch failed", exc=exc)
        return ArchivistDigest(
            question=question,
            tag_expr=resolved_tag_expr,
            exclude_tags=exclude_list,
            citations=[],
            no_coverage=True,
            distinct_pages=0,
        )

    citations: list[CitationSnippet] = []
    for excerpt in out.excerpts:
        # PageExcerpt.spans is the canonical v0.33.0 surface; fall back
        # to ``parse_spans`` only if a legacy blob sneaks through.
        spans = (
            excerpt.spans if isinstance(excerpt.spans, list) else parse_spans(str(excerpt.spans))
        )
        chosen = pick_first_prose_span(spans)
        if chosen is None:
            continue
        snippet_text = truncate_at_word_boundary(chosen.body.strip(), 200)
        if not snippet_text:
            continue
        citations.append(
            CitationSnippet(
                collection=excerpt.collection,
                slug=excerpt.slug,
                title=excerpt.title,
                snippet=snippet_text,
            )
        )

    # ``no_coverage`` per the F19 ground-shape contract (spec §2):
    # ``True`` when the corpus is non-empty AND no hits landed — the
    # scope-miss-on-populated-wiki case that distinguishes a populated
    # wiki that lacks the topic from an empty corpus. ``LibrarianOutput``
    # at v0.33.0 lacks a ``corpus_page_count`` field, so the ground
    # function probes the catalog directly. The probe is best-effort:
    # any failure (wiki unresolvable, catalog unreadable, missing
    # wiki_dir) leaves ``no_coverage=False`` rather than raising — the
    # dispatch-exception branch above already handled the hard-error
    # case. Helper is module-scope so tests can monkeypatch it without
    # touching the catalog.
    no_coverage = False
    if not citations:
        try:
            corpus_size = _corpus_page_count()
        except Exception:
            corpus_size = 0
        no_coverage = corpus_size > 0

    return ArchivistDigest(
        question=question,
        tag_expr=resolved_tag_expr,
        exclude_tags=exclude_list,
        citations=citations,
        no_coverage=no_coverage,
        distinct_pages=len({c.slug for c in citations}),
    )


def _corpus_page_count() -> int:
    """Return the active wiki's wiki-section page count.

    Probes ``<wiki_dir>/.lies/catalog.db`` for ``section='wiki'``
    rows. The wiki section is the right corpus for the grounding
    digest — library mirrors live under ``section='ingested'`` and
    are not the wiki corpus. Best-effort: any exception (wiki not
    registered, catalog unreadable, no active wiki) propagates and
    the caller falls back to ``no_coverage=False``.

    Lazy-imported dependencies keep this helper off the bare
    pydantic_ai / fastmcp import path so ``ground()`` is cheap on
    the happy path.
    """
    from lies.mcp.resolution import resolve_wiki
    from lies.memory.catalog import list_pages, open_catalog
    from lies.memory.catalog_models import PageSection

    wiki = resolve_wiki()
    conn = open_catalog(wiki)
    try:
        return len(list_pages(conn, section=PageSection.wiki))
    finally:
        conn.close()
