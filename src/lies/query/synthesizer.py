"""Query synthesizer with a qmd → wiki/index.md fallback.

Implements the query workflow from the LIES default schema: search via ``qmd query``
(hybrid BM25 + vector + rerank), read the top-N pages (default 5), synthesize an answer
with inline citations. On ``qmd`` not installed or no results it falls back to
``wiki/index.md`` navigation.

Synthesis here is deterministic and extractive so the fallback path is testable without a
live LLM. The LLM-backed path lives in ``Orchestrator._call_query_synthesizer``, which
calls this module on failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from lies.library.registry import LibraryCollectionMeta
from lies.markdown_spans import Span, parse_spans
from lies.qmd.cli import qmd_query
from lies.query.index_parser import parse_index_links
from lies.query.models import SynthesizedAnswer
from lies.query.section import _extract_section_at
from lies.query.tag_expr import (
    And,
    Include,
    Or,
    ResolvedTagFilter,
)
from lies.wiki.wiki import Wiki

DEFAULT_TOP_N = 5

# Reasons the fallback was triggered (also see SynthesizedAnswer docs).
FALLBACK_REASON_UNAVAILABLE = "qmd_unavailable"
FALLBACK_REASON_NO_RESULTS = "qmd_no_results"
FALLBACK_REASON_FAILED = "qmd_failed"
# Library pass returned 0 hits but the wiki pass surfaced content. The
# answer is not grounded in primary (library) sources — see the
# Bundle-C preamble that callers render when this reason is set.
FALLBACK_REASON_WIKI_ONLY = "wiki_only"

# Qmd search callable signature: (cwd, question, limit) -> list[dict].
QmdSearchFn = Callable[..., list[dict[str, object]]]

# Indirection over the qmd search callable: the public functions look it up through
# ``_qmd_search_default()`` at *call* time rather than via a captured default argument.
# A captured default would shadow module-level ``monkeypatch.setattr`` patches, so every
# test would shell out to the real qmd binary — ~40s per test where qmd is installed.
_QMD_SEARCH: QmdSearchFn = qmd_query


def set_qmd_search(fn: QmdSearchFn) -> None:
    """Replace the qmd search callable used by default.

    Tests use this to stub the real ``qmd_query`` binary without
    patching module attributes (whose captures in default arguments
    would otherwise shadow the rebind). Production code never calls
    this; the default value is the real ``qmd_query`` at import time.
    """
    global _QMD_SEARCH
    _QMD_SEARCH = fn


def _qmd_search_default() -> QmdSearchFn:
    """Return the currently-bound qmd search callable."""
    return _QMD_SEARCH


@dataclass(frozen=True)
class PageRead:
    """A page read during retrieval.

    ``source`` discriminates between the two physical roots the
    dispatcher reads from. Same path from both roots produces two
    distinct PageRead objects; the operator sees both with source
    tags in the answer body.

    ``spans`` (F19): structured view of the page body, replacing the
    single-paragraph ``excerpt`` field. Synthesizer picks per-claim
    span from this list. Code-fence spans are emitted with
    ``code_fence=True``; downstream consumers exclude them from
    prose excerpts.
    """

    rel_path: str  # wiki-relative, POSIX
    title: str
    spans: list[Span]
    source: str  # "library" | "wiki" — required, no default (hard cutover)
    line: int | None = None  # qmd's per-hit line (1-indexed); None for index-fallback paths
    section: str | None = None  # last ATX heading at or before `line`


def retrieve_pages(
    question: str,
    wiki: Wiki,
    *,
    top_n: int = DEFAULT_TOP_N,
    qmd_search: QmdSearchFn | None = None,
    tag_filter: ResolvedTagFilter | None = None,
) -> tuple[list[PageRead], str]:
    """Retrieve the candidate pages for ``question``.

    Runs two ``qmd_query`` passes when both roots are populated: a **primary** pass scoped to
    the resolved library collection set (the source of truth) and a **secondary** pass scoped
    to the wiki-rooted collection, which surfaces local edits as supplementary material. Both
    resolve through ``_resolve_qmd_pages``, which prefers the library root per path; pages
    carry ``source`` so the synthesizer can apply library-wins-on-conflict at answer time.

    ``tag_filter`` is resolved against the library registry and forwarded as
    ``collection_filter`` for the **primary pass** only: the secondary pass always uses the
    wiki-rooted collection, because local wiki content is supplementary. Returns
    ``(pages, fallback_reason)``, the reason being ``""`` when qmd served the query.
    """
    pages: list[PageRead] = []
    fallback_reason = ""

    qmd_search_fn = qmd_search if qmd_search is not None else _qmd_search_default()
    collection_filter: set[str] | None = None
    if tag_filter is not None:
        collection_filter = _collections_matching(tag_filter)

    wiki_collection = f"wiki_{wiki.name}"
    wiki_filter: set[str] = {wiki_collection}

    primary_pages: list[PageRead] = []
    wiki_pages: list[PageRead] = []
    primary_failure: str | None = None
    wiki_failure: str | None = None

    # Primary: library collections.
    try:
        primary_pages = _qmd_search_dispatch(
            qmd_search_fn,
            wiki,
            question,
            top_n,
            collection_filter=collection_filter,
        )
    except _QmdUnavailable:
        primary_failure = FALLBACK_REASON_UNAVAILABLE
    except _QmdNoResults:
        primary_failure = FALLBACK_REASON_NO_RESULTS
    except _QmdOtherFailure:
        primary_failure = FALLBACK_REASON_FAILED

    # Secondary: wiki-rooted collection. Always attempted; the wiki
    # may carry content even when library is empty (or vice versa).
    try:
        wiki_pages = _qmd_search_dispatch(
            qmd_search_fn,
            wiki,
            question,
            top_n,
            collection_filter=wiki_filter,
        )
    except _QmdUnavailable:
        wiki_failure = FALLBACK_REASON_UNAVAILABLE
    except _QmdNoResults:
        wiki_failure = FALLBACK_REASON_NO_RESULTS
    except _QmdOtherFailure:
        wiki_failure = FALLBACK_REASON_FAILED

    pages = primary_pages + wiki_pages

    # Dedupe on ``(rel_path, source)``: the wiki resolver surfaces a wiki-only path from both
    # passes. Without the dedupe the body says "Based on 2 wiki page(s)" while ``page_texts``
    # has one entry, and the answer contradicts itself.
    deduped_pages: list[PageRead] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        key = (page.rel_path, page.source)
        if key in seen:
            continue
        seen.add(key)
        deduped_pages.append(page)
    pages = deduped_pages

    if not pages:
        # Decide fallback reason from the failure pattern.
        if (
            primary_failure == FALLBACK_REASON_UNAVAILABLE
            or wiki_failure == FALLBACK_REASON_UNAVAILABLE
        ):
            fallback_reason = FALLBACK_REASON_UNAVAILABLE
        elif primary_failure == FALLBACK_REASON_FAILED or wiki_failure == FALLBACK_REASON_FAILED:
            fallback_reason = FALLBACK_REASON_FAILED
        elif (
            primary_failure == FALLBACK_REASON_NO_RESULTS
            and wiki_failure == FALLBACK_REASON_NO_RESULTS
        ):
            fallback_reason = FALLBACK_REASON_NO_RESULTS
        elif primary_failure and not wiki_failure:
            # Library failed but wiki succeeded? Per the dispatch above
            # this case can't reach ``pages == []`` — wiki_pages would
            # be non-empty. Defensive default.
            fallback_reason = primary_failure
        elif not primary_failure and wiki_failure:
            # Library succeeded (returned empty list, not an exception),
            # wiki raised. Treat as library empty.
            fallback_reason = FALLBACK_REASON_NO_RESULTS
        else:
            fallback_reason = FALLBACK_REASON_NO_RESULTS
    elif not primary_pages and wiki_pages:
        # Library returned nothing; wiki surfaced content. Mark
        # answer as not grounded in primary sources.
        fallback_reason = FALLBACK_REASON_WIKI_ONLY
    # else: at least one primary page exists; no fallback flag.

    return pages, fallback_reason


def synthesize_answer(
    question: str,
    wiki: Wiki,
    *,
    top_n: int = DEFAULT_TOP_N,
    qmd_search: QmdSearchFn | None = None,
) -> SynthesizedAnswer:
    """Answer ``question`` using the wiki at ``wiki``, returning a :class:`SynthesizedAnswer`
    whose ``fallback_used`` / ``fallback_reason`` describe how it was built.

    Tries ``qmd_search`` first; on any qmd failure it falls back to the top-N pages
    referenced by ``wiki/index.md``. Deterministic and extractive, with no LLM round-trip;
    retrieval is delegated to :func:`retrieve_pages` so the orchestrator shares it.

    Args:
        question: The user's natural-language question.
        wiki: The wiki to search.
        top_n: Maximum pages to read (default 5, per schema).
        qmd_search: Search callable; defaults to the module-level indirection.
    """
    if not question or not question.strip():
        return SynthesizedAnswer(answer="(empty question)")

    pages, fallback_reason = retrieve_pages(question, wiki, top_n=top_n, qmd_search=qmd_search)

    if not pages:
        return SynthesizedAnswer(
            answer=_empty_answer(question, fallback_reason),
            citations=[],
            pages_read=[],
            fallback_used=bool(fallback_reason),
            fallback_reason=fallback_reason,
            format="md",
        )

    return build_answer_from_pages(question, pages, fallback_reason)


# ---------------------------------------------------------------------------
# Internal sentinel exceptions (so the public function catches a narrow set)
# ---------------------------------------------------------------------------


class _QmdUnavailable(Exception):
    """Sentinel: qmd binary is missing."""


class _QmdNoResults(Exception):
    """Sentinel: qmd returned zero results or paths we couldn't read."""


class _QmdOtherFailure(Exception):
    """Sentinel: qmd exited non-zero / timed out / bad JSON."""


# ---------------------------------------------------------------------------
# Index-driven page reading
# ---------------------------------------------------------------------------


def _read_pages_from_index(wiki: Wiki, top_n: int) -> list[PageRead]:
    """Read the top-N pages referenced by ``wiki/index.md``.

    Only the first ``top_n`` *existing* pages are returned, in index
    order (which the catalog port keeps alphabetical within each section).
    """
    if not (wiki.wiki_dir / "index.md").exists():
        return []

    content = (wiki.wiki_dir / "index.md").read_text(encoding="utf-8")
    links = parse_index_links(content)

    pages: list[PageRead] = []
    for link in links:
        if len(pages) >= top_n:
            break
        # The index stores paths relative to wiki/, e.g., "entities/postgres.md"
        page_on_disk = wiki.wiki_dir / link.path
        read = _try_read(page_on_disk, wiki, title_override=link.title)
        if read is not None:
            pages.append(read)
    return pages


def _resolve_qmd_pages(
    wiki: Wiki,
    qmd_hits: list[dict[str, object]],
    top_n: int,
) -> list[PageRead]:
    """Resolve qmd hits to actual readable pages on disk.

    Each hit is matched against both physical roots: the library ``collections_root`` (the
    canonical source of truth) and ``wiki.wiki_dir`` (local override / author-only content).
    The same path from both roots yields two distinct ``PageRead`` objects with distinct
    ``source`` fields, and the LLM step applies library-wins-on-conflict at answer time.

    A library hit reports its qmd URI form as ``rel_path``, because library files live outside
    ``wiki.data_root``. Each hit's ``line`` is threaded into the ``PageRead`` and ``section``
    is derived by scanning for the last ATX heading at or before it. A candidate that escapes
    either root is dropped.
    """
    pages: list[PageRead] = []
    for hit in qmd_hits:
        if len(pages) >= top_n:
            break
        raw = hit.get("path")
        if not isinstance(raw, str):
            continue
        raw_line = hit.get("line")
        hit_line: int | None = raw_line if isinstance(raw_line, int) else None
        # Library first: primary source of truth.
        lib_path = _resolve_qmd_path_in_library(wiki, raw)
        if lib_path is not None:
            lib_page = _build_library_page_read(lib_path, hit_line=hit_line)
            if lib_page is not None:
                pages.append(lib_page)
        # Wiki: surfaces overrides and collision cases. Library is
        # canonical but a wiki file at the same path is preserved as a
        # distinct PageRead for the local-override rule.
        if len(pages) >= top_n:
            continue
        wiki_path = _resolve_qmd_path_in_wiki(wiki, raw)
        if wiki_path is not None:
            wiki_page = _try_read(wiki_path, wiki, hit_line=hit_line)
            if wiki_page is not None:
                pages.append(wiki_page)
    return pages


def _resolve_qmd_path_in_library(wiki: Wiki, raw: str) -> Path | None:
    """Resolve ``raw`` strictly under ``Library.collections_root``.

    Library files live at ``<coll>/<file>`` relative to the
    collections root, so the qmd hit's first path segment is the
    collection name. An absolute path or a path with no first segment
    yields no library candidate (the path is wiki-only by
    construction).

    Path traversal defense: any candidate that escapes
    ``Library.collections_root`` is dropped (returns None).
    """
    if Path(raw).is_absolute():
        return None
    first, _, rest = raw.partition("/")
    if not first:
        return None
    # Local import to avoid a circular import at module load time
    # (matches the existing pattern for qmd.cli imports below).
    from lies.library.paths import Library  # noqa: PLC0415

    lib_root = Library.open().collections_root
    lib_candidate = (lib_root / first / rest).resolve()
    try:
        lib_candidate.relative_to(lib_root.resolve())
    except ValueError:
        return None
    return lib_candidate if lib_candidate.is_file() else None


def _resolve_qmd_path_in_wiki(wiki: Wiki, raw: str) -> Path | None:
    """Resolve ``raw`` strictly under ``wiki.wiki_dir``.

    The wiki-rooted qmd collection registers at ``wiki.wiki_dir``, and every wiki-pass hit is
    normalized into the qmd URI form minus the ``qmd://`` prefix, so the path's first segment
    is the collection name; joining that on blindly would land one level too deep, where
    nothing exists.

    Strip the ``wiki_<wikiname>/`` prefix when present and leave other paths unchanged, so a
    path lacking the prefix still resolves. A candidate that escapes ``wiki.wiki_dir`` is
    dropped (returns None).
    """
    wiki_prefix = f"wiki_{wiki.name}/"
    if raw.startswith(wiki_prefix):
        stripped = raw[len(wiki_prefix) :]
    else:
        stripped = raw
    candidate = Path(stripped)
    if candidate.is_absolute():
        wiki_candidate = candidate
    else:
        wiki_candidate = (wiki.wiki_dir / stripped).resolve()
    try:
        wiki_candidate.relative_to(wiki.wiki_dir.resolve())
    except ValueError:
        return None
    return wiki_candidate if wiki_candidate.is_file() else None


def _build_library_page_read(
    path: Path,
    *,
    hit_line: int | None = None,
) -> PageRead | None:
    """Build a ``PageRead`` for a library-side path.

    ``rel_path`` is the qmd URI form (``<coll>/<file>``) — library
    files live under ``Library.collections_root`` which is NOT under
    ``wiki.data_root``, so a data_root-relative path would lie about
    filesystem layout. Returns ``None`` on read failure (mirrors
    :func:`_try_read`).

    ``spans`` is the structured parse of the page body (F19). The
    ``section`` field is preserved for back-compat with the
    pre-F19 single-line section derivation.
    """
    # Local import: same rationale as ``_resolve_qmd_path_in_library``.
    from lies.library.paths import Library  # noqa: PLC0415

    try:
        content = path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None
    lib_root = Library.open().collections_root.resolve()
    rel = path.relative_to(lib_root).as_posix()
    title = _extract_title(content) or path.stem
    spans = parse_spans(content)
    section = _extract_section_at(content, hit_line) if hit_line is not None else None
    return PageRead(
        rel_path=rel,
        title=title,
        spans=spans,
        source="library",
        line=hit_line,
        section=section,
    )


def _try_read(
    path: Path,
    wiki: Wiki,
    *,
    title_override: str | None = None,
    hit_line: int | None = None,
) -> PageRead | None:
    """Read a page; return None if missing/unreadable.

    Pages resolved from ``wiki.wiki_dir`` carry ``source="wiki"``.
    Library-sourced pages are constructed by callers via the new
    library-resolution helper (Task 3) with ``source="library"``.

    When ``hit_line`` is provided, ``section`` is computed via
    ``_extract_section_at`` from the page body.
    """
    if not path.exists() or not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None

    rel = path.relative_to(wiki.data_root).as_posix()
    title = title_override or _extract_title(content) or path.stem
    spans = parse_spans(content)
    section = _extract_section_at(content, hit_line) if hit_line is not None else None
    return PageRead(
        rel_path=rel,
        title=title,
        spans=spans,
        source="wiki",
        line=hit_line,
        section=section,
    )


def _extract_title(content: str) -> str | None:
    """Return the first H1 heading text, skipping YAML frontmatter."""
    in_fm = False
    seen_fm = False
    for line in content.splitlines():
        if not seen_fm and line.strip() == "---":
            in_fm = not in_fm
            if not in_fm:
                seen_fm = True
            continue
        if in_fm:
            continue
        if line.startswith("# "):
            return line[2:].strip()
    return None


# ---------------------------------------------------------------------------
# Answer assembly
# ---------------------------------------------------------------------------


def build_answer_from_pages(
    question: str,
    pages: list[PageRead],
    fallback_reason: str,
    *,
    format_hint: Literal["md", "table", "marp", "chart"] = "md",
) -> SynthesizedAnswer:
    """Assemble the final SynthesizedAnswer from already-retrieved ``pages``, returning a
    :class:`SynthesizedAnswer` whose ``format`` is the validator's verdict on ``format_hint``.

    Public seam for the extractive answer builder: callers that already called
    :func:`retrieve_pages` pass the resulting ``pages`` through here, so the fallback path does
    not pay for a second qmd / index scan. ``pages`` may be empty, in which case the answer
    has empty citations and a body describing why.

    Args:
        question: The user's natural-language question.
        pages: Already-retrieved pages; empty is valid and explained in the body.
        format_hint: Auto-routed format intent, validated by :func:`validate_format`.
    """
    from lies.query.citation import Citation
    from lies.query.format_validator import validate_format

    citations: list[Citation] = []
    pages_read: list[Citation] = []
    page_links: list[str] = []
    bullets: list[str] = []
    for page in pages:
        # ``PageRead.source`` is typed ``str`` for ease of construction; ``Citation.source``
        # narrows to ``Literal["library", "wiki"]`` at the data-shape boundary. Both values come
        # from the same closed set, so the cast is safe.
        c = Citation(
            path=page.rel_path,
            source=cast(Literal["library", "wiki"], page.source),
        )
        citations.append(c)
        pages_read.append(c)
        page_links.append(f"[{page.title}]({page.rel_path})")
        # F19: pick the first non-empty prose span as the per-page excerpt.
        prose_span = next(
            (s for s in page.spans if s.body.strip() and not s.code_fence),
            None,
        )
        if prose_span is None:
            excerpt = "(no extractable content)"
            heading_path: list[str] = []
        else:
            # Trim trailing whitespace + cap at first paragraph
            excerpt = prose_span.body.strip().split("\n\n", 1)[0]
            heading_path = prose_span.heading_path
        heading_str = " > ".join(heading_path) if heading_path else "top of page"
        # Per-page bullet uses the [[slug]]: "verbatim" form (F19). For
        # multi-page summaries this gives each citation a heading context;
        # for the [[slug]] rendering we use the bare slug (no collection
        # prefix).
        bare_slug = page.rel_path.split("/", 1)[1] if "/" in page.rel_path else page.rel_path
        bare_slug = bare_slug.removesuffix(".md")
        bullets.append(f'- [[{bare_slug}]] ({heading_str}): "{excerpt}"')

    if fallback_reason == FALLBACK_REASON_WIKI_ONLY:
        preamble = (
            "_Note: not grounded in primary sources (library returned no matches); "
            "answered from wiki._\n\n"
        )
    elif fallback_reason:
        preamble = (
            f"_Note: qmd unavailable ({fallback_reason}); answered from `wiki/index.md`._\n\n"
        )
    else:
        preamble = ""

    answer = (
        f"### {question.strip()}\n\n"
        f"{preamble}"
        f"Based on {len(pages)} wiki page(s):\n\n" + "\n".join(bullets)
    )

    validated_format = (
        format_hint if format_hint == "chart" else validate_format(answer, format_hint)
    )

    return SynthesizedAnswer(
        answer=answer,
        citations=citations,
        pages_read=pages_read,
        fallback_used=bool(fallback_reason),
        fallback_reason=fallback_reason,
        page_links=page_links,
        format=validated_format,
    )


def _empty_answer(question: str, fallback_reason: str) -> str:
    """The 'no pages found' answer body.

    Renders when both qmd passes failed to surface any readable page.
    The two-pass refactor retired the ``wiki/index.md`` fallback, so the
    body now states the qmd failure reason (e.g. ``qmd_unavailable``)
    without promising that the index was tried.

    The ``FALLBACK_REASON_WIKI_ONLY`` branch is defensive: ``WIKI_ONLY``
    is only set when the wiki pass *did* surface readable pages, so
    this function is unreachable in that case. Kept for symmetry with
    :func:`build_answer_from_pages` so the operator-facing message set
    is closed.
    """
    if fallback_reason == FALLBACK_REASON_NO_RESULTS:
        return (
            f"### {question.strip()}\n\n"
            f"_qmd query returned no results ({fallback_reason}); "
            "no readable pages._\n\n"
            "No pages found."
        )
    if fallback_reason == FALLBACK_REASON_WIKI_ONLY:
        return (
            f"### {question.strip()}\n\n"
            f"_Not grounded in primary sources (library returned no matches) "
            f"({fallback_reason}); no readable pages._\n\n"
            "No pages found."
        )
    if fallback_reason == FALLBACK_REASON_UNAVAILABLE:
        return (
            f"### {question.strip()}\n\n"
            f"_qmd is not installed ({fallback_reason}); "
            "no readable pages._\n\n"
            "No pages found."
        )
    if fallback_reason == FALLBACK_REASON_FAILED:
        return (
            f"### {question.strip()}\n\n"
            f"_qmd query failed ({fallback_reason}); "
            "no readable pages._\n\n"
            "No pages found."
        )
    return f"### {question.strip()}\n\nNo pages found."


# ---------------------------------------------------------------------------
# Qmd dispatch: translate real qmd exceptions into sentinels
# ---------------------------------------------------------------------------


def _qmd_search_dispatch(
    fn: QmdSearchFn,
    wiki: Wiki,
    question: str,
    top_n: int,
    *,
    collection_filter: set[str] | None = None,
) -> list[PageRead]:
    """Call ``fn`` and translate its real exceptions into sentinels.

    The public ``synthesize_answer`` only catches the sentinel exceptions, so the real
    ``lies.qmd.cli`` types are mapped here and the public surface stays narrow.

    ``collection_filter`` is the resolved set of collection names the caller wants to scope to,
    passed through so a callable supporting per-collection filtering (notably
    :func:`lies.qmd.cli.qmd_query`) can act on it. Stubs using ``*args, **kwargs`` ignore it.
    """
    # Local imports avoid a circular import at module load time.
    from lies.qmd.cli import (  # noqa: PLC0415
        QmdCommandError,
        QmdNoResultsError,
        QmdNotInstalledError,
    )

    try:
        results = fn(
            wiki.data_root,
            question,
            top_n,
            collection_filter=collection_filter,
        )
    except QmdNotInstalledError as exc:
        raise _QmdUnavailable(str(exc)) from exc
    except QmdNoResultsError as exc:
        raise _QmdNoResults(str(exc)) from exc
    except QmdCommandError as exc:
        raise _QmdOtherFailure(str(exc)) from exc

    # Carry qmd hit metadata through to _resolve_qmd_pages so each
    # PageRead can capture the line number qmd returned. Hits without
    # a `line` key still surface; their PageRead carries line=None.
    qmd_hits: list[dict[str, object]] = [
        r for r in results if isinstance(r, dict) and isinstance(r.get("path"), str)
    ]
    pages = _resolve_qmd_pages(wiki, qmd_hits, top_n)
    if not pages:
        # qmd gave us hits but none of the files are readable — treat as
        # "no results" so the fallback path runs.
        raise _QmdNoResults("qmd returned no readable pages")
    return pages


def _collections_matching(tag_filter: ResolvedTagFilter) -> set[str]:
    """Return the set of library collection names that pass ``tag_filter``.

    Library collections are the source of truth for tag expression resolution; wikis do not own
    collections. A ``+c:opencode`` filter matches every library collection named ``opencode``,
    regardless of which wiki the operator's MCP daemon is bound to.

    Implicit self-tag: a collection matches if its ``name`` is in ``tags ∪ {name}``. The rule
    lives here at the retriever boundary, not in the resolver — the resolver only validates
    that every atom is in the available tag set.

    F15 qualifier dispatch: ``t`` (or none) uses the implicit-self-tag rule, ``c`` is a strict
    name match, and exclude wins on collision. Returns the set of *names*.
    """
    from lies.library.registry import library_collection_metas
    from lies.query.tag_expr import atom_matches, exclude_matches

    matching: set[str] = set()

    exclude = tag_filter.exclude
    include = tag_filter.include

    def _eval_include(node: object, coll: LibraryCollectionMeta) -> bool:
        if isinstance(node, Include):
            return atom_matches(coll, node)
        if isinstance(node, And):
            return _eval_include(node.left, coll) and _eval_include(node.right, coll)
        if isinstance(node, Or):
            return _eval_include(node.left, coll) or _eval_include(node.right, coll)
        return False

    for coll in library_collection_metas():
        if exclude is not None and exclude_matches(coll, exclude):
            continue
        if include is None:
            matching.add(coll.name)
            continue
        if _eval_include(include, coll):
            matching.add(coll.name)
    return matching


def _all_collection_names() -> list[str]:
    """Every collection in the library, sorted by name.

    The "no tag_filter" scope per the Bundle C spec
    (§"Retriever consumption" — "without a filter, the scope is all
    registered collections"). The library is the universe; wikis do
    not contribute to the addressable collection set.
    """
    from lies.library.registry import library_collection_names

    return sorted(library_collection_names())


def _searched_scope(wiki: Wiki, tag_filter: ResolvedTagFilter | None) -> list[str]:
    """The ``searched_scope`` list for an answer against ``wiki``.

    With a filter: the resolved collection set from :func:`_collections_matching` (sorted,
    unique). Without: every collection registered in the library per
    :func:`_all_collection_names`. Empty in either case when nothing is registered.

    The orchestrator populates ``SynthesizedAnswer.searched_scope`` from this helper so
    downstream surfaces can react to the effective scope. The ``wiki`` parameter is retained
    for signature uniformity; the implementation reads the library, the source of truth.
    """
    if tag_filter is not None:
        return sorted(_collections_matching(tag_filter))
    return _all_collection_names()
