"""Query synthesizer with qmd → wiki/index.md fallback.

Implements the query workflow from the LIES default schema (and the
Error handling table in the spec):

    1. Search via `qmd query` (hybrid BM25 + vector + rerank).
    2. Read the top-N pages (default 5).
    3. Synthesize an answer with inline citations.

Fallback (per spec):

    - `qmd` not installed → fall back to `wiki/index.md` navigation.
    - `qmd query` returns no results → fall back to `wiki/index.md` scan.

The synthesis here is deterministic / extractive so the fallback path
is testable without a live LLM. The LLM-backed synthesis now lives in
``Orchestrator._call_query_synthesizer`` and calls this function on
failure; the public function signature and the fallback contract are
the load-bearing parts.
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

# Indirection over the qmd search callable. The public
# :func:`retrieve_pages` and :func:`synthesize_answer` look the callable
# up through ``_qmd_search_default()`` at *call* time rather than via a
# captured default argument, so tests can rebind it with
# :func:`set_qmd_search` without having to rewrite ``__defaults__``.
# The captured-default form would silently shadow module-level
# ``monkeypatch.setattr`` patches — every test would shell out to the
# real qmd binary instead of the stub, costing ~40s/test on systems
# where qmd is installed.
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

    Runs two ``qmd_query`` passes when both roots are populated:

    1. **Primary pass**: qmd scoped to the resolved library collection
       set (``collection_filter`` derived from ``tag_filter``).
       Library collections are the primary source of truth.
    2. **Secondary pass**: qmd scoped to the wiki-rooted collection
       (``wiki_<wikiname>``). Surfaces local edits and author-only
       content as supplementary material.

    Both result sets resolve through ``_resolve_qmd_pages``, which
    prefers the library root and falls back to the wiki root for
    each path. Pages are tagged with ``source`` so the synthesizer
    can apply the library-wins-on-conflict rule at answer time.

    This is the single retrieval path for the query layer. Both the
    extractive ``synthesize_answer`` and the orchestrator's LLM
    synthesis consume it, so the agent and its extractive fallback can
    never disagree about which pages were read.

    ``qmd_search`` defaults to the module-level indirection over
    ``lies.qmd.cli.qmd_query`` (rebindable via :func:`set_qmd_search`),
    not a captured default argument. Tests stub the indirection; the
    indirection is looked up at call time so a stub rebind actually
    takes effect for subsequent calls.

    ``tag_filter`` (Task 6 / Bundle C) carries the resolved
    include/exclude expression the caller (CLI or MCP) wants scoped to.
    The retriever resolves the filter against the wiki's collection
    registry (``wiki.collections_dir/*.yaml``) via
    :func:`_collections_matching` and forwards the resulting set as
    ``collection_filter`` to ``qmd_search`` for the **primary pass**.
    The secondary pass always uses the wiki-rooted ``wiki_<name>``
    collection, independent of ``tag_filter`` — local wiki content is
    always supplementary, never gated by the caller's filter.

    The implicit-self-tag rule (a collection's name is always an
    addressable tag regardless of its ``tags`` field) lives at this
    boundary, not in :func:`lies.query.tag_expr.resolve`. The resolver
    only validates that every atom exists in the available set; the
    per-collection semantics are the retriever's concern.

    Fallback reasons:

    - ``""`` — qmd served at least one pass and we have readable pages.
    - ``FALLBACK_REASON_UNAVAILABLE`` — qmd binary missing.
    - ``FALLBACK_REASON_FAILED`` — qmd errored on both passes.
    - ``FALLBACK_REASON_NO_RESULTS`` — both passes returned 0 hits.
    - ``FALLBACK_REASON_WIKI_ONLY`` — library returned 0, wiki
      returned hits. Answer is not grounded in primary sources.

    Returns:
        ``(pages, fallback_reason)``. ``fallback_reason`` is ``""``
        when qmd served the query, else one of the ``FALLBACK_REASON_*``
        constants. ``pages`` may be empty when nothing was readable.
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

    # Dedupe on ``(rel_path, source)`` so the same wiki-only path
    # returned by both qmd passes (the library resolver drops the path
    # when no library mirror exists; the wiki resolver surfaces it from
    # both passes because both routes resolve under wiki.wiki_dir) does
    # not yield duplicate ``PageRead`` objects. The orchestrator's
    # ``pages_read`` / ``page_links`` lists and the extractive body
    # count must agree — without the dedup, the body says "Based on 2
    # wiki page(s)" while ``page_texts`` (the dict the synthesizer
    # agent reads) has only one entry, contradicting itself.
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
    """Answer `question` using the wiki at `wiki`.

    Tries ``qmd_search`` first. On ``QmdNotInstalledError``,
    ``QmdNoResultsError``, or any other qmd failure, falls back to
    reading the top-N pages referenced by ``wiki/index.md``.

    Deterministic and extractive: no LLM round-trip. Retrieval is
    delegated to :func:`retrieve_pages` so the orchestrator's LLM
    synthesis path shares it.

    Args:
        question: The user's natural-language question.
        wiki: The wiki to search.
        top_n: Maximum number of pages to read (default 5, per schema).
        qmd_search: Injectable search callable. Defaults to the
            module-level indirection over :func:`lies.qmd.cli.qmd_query`
            (rebindable via :func:`set_qmd_search`). Tests pass a stub
            either as a kwarg or by rebinding the indirection for the
            test scope.

    Returns:
        A :class:`SynthesizedAnswer` whose ``fallback_used`` and
        ``fallback_reason`` fields describe how the answer was built.
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

    Each qmd hit is matched against both physical roots:

    - ``Library.collections_root/<first-segment>/<rest>`` — library is
      the canonical source of truth.
    - ``wiki.wiki_dir/<rest>`` — wiki is the local override / author-
      only content.

    The same path from both roots yields two distinct ``PageRead``
    objects with distinct ``source`` fields; the LLM synthesis step
    applies the library-wins-on-conflict rule at answer time. A
    library hit is reported with its qmd URI form (``<coll>/<file>``)
    as ``rel_path`` because library files live outside ``wiki.data_root``
    — a data_root-relative path would lie about filesystem layout.

    Each hit's ``line`` field (1-indexed qmd line of best match) is
    threaded into the constructed ``PageRead``. ``section`` is then
    derived by reading the page body and scanning for the last ATX
    heading at or before that line via ``_extract_section_at``.

    Defends against path traversal per root: any candidate that escapes
    its root is dropped, the next root is tried.
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

    The wiki-rooted qmd collection (``wiki_<wikiname>`` per
    :meth:`WikiLayout.init`) registers at ``wiki.wiki_dir``. Every
    qmd hit from the wiki pass is normalized into the qmd URI form
    minus the ``qmd://`` prefix, so the path's first segment is the
    collection name (``wiki_<wikiname>/<rest>``). Joining that onto
    ``wiki.wiki_dir`` blindly would land at
    ``wiki.wiki_dir / wiki_<wikiname> / <rest>`` — which doesn't exist
    (the real file is at ``wiki.wiki_dir / <rest>``).

    Strip the ``wiki_<wikiname>/`` prefix when present; leave other
    paths unchanged so the wiki pass still resolves paths that lack
    the prefix (defensive against a qmd shape change or a stub
    fixture).

    Mirrors the path-traversal defense the pre-Task-3 helper
    enforced: any candidate that escapes ``wiki.wiki_dir`` is
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
    except (OSError, UnicodeDecodeError):
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
    except (OSError, UnicodeDecodeError):
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


def _excerpt_from_spans(spans: list[Span], max_chars: int = 400) -> str:
    """Return a short prose excerpt from the first non-code-fence span.

    Used by :func:`build_answer_from_pages` as a placeholder until the
    F19 per-claim span picker (Task 5+) replaces this single-bullet
    excerpt with a synthesized body that picks one span per claim.
    Code-fence spans are excluded because their contents are not prose.
    """
    for span in spans:
        if span.code_fence:
            continue
        text = span.body.strip()
        if not text:
            continue
        if len(text) > max_chars:
            text = text[: max_chars - 3].rstrip() + "..."
        return text
    return "(no extractable content)"


# ---------------------------------------------------------------------------
# Answer assembly
# ---------------------------------------------------------------------------


def build_answer_from_pages(
    question: str,
    pages: list[PageRead],
    fallback_reason: str,
    *,
    format_hint: Literal["md", "table", "marp"] = "md",
) -> SynthesizedAnswer:
    """Assemble the final SynthesizedAnswer from already-retrieved ``pages``.

    Public seam for the extractive answer builder: callers (notably
    :meth:`lies.orchestrator.Orchestrator.run_query`) that have already
    called :func:`retrieve_pages` once pass the resulting ``pages``
    through here so the fallback path doesn't pay for a second qmd /
    index scan. ``pages`` may be empty, in which case the returned
    answer has empty citations / pages_read / page_links and a body
    describing why.

    Each ``PageRead`` carries a ``source`` discriminator
    (``"library"`` / ``"wiki"``); we wrap it in a
    :class:`lies.query.citation.Citation` so the answer carries the
    source through to downstream consumers.

    Args:
        question: The user's natural-language question.
        pages: The pages already retrieved for ``question``. Empty is
            valid; the returned body explains what was missing.
        fallback_reason: One of the ``FALLBACK_REASON_*`` constants;
            empty when qmd served the query.
        format_hint: The synthesizer's auto-routed format intent
            (``"md"`` / ``"table"`` / ``"marp"``). Validated against
            the answer body via
            :func:`lies.query.format_validator.validate_format`; on
            parse failure the validator silently demotes to ``"md"``.
            Defaults to ``"md"`` so the orchestrator's no-pages and
            LLM-failure fallback paths keep compiling without
            forwarding a hint.

    Returns:
        A :class:`SynthesizedAnswer` whose ``fallback_used`` /
        ``fallback_reason`` mirror the inputs and whose ``format``
        field is the validator's verdict on ``format_hint``.
        ``synthesis_used`` is always False here — the orchestrator
        wraps this with the synthesis metadata (``synthesis_used``,
        ``synthesis_reason``) since this function has no opinion on
        whether the LLM was invoked.
    """
    from lies.query.citation import Citation
    from lies.query.format_validator import validate_format

    citations: list[Citation] = []
    pages_read: list[Citation] = []
    page_links: list[str] = []
    bullets: list[str] = []
    for page in pages:
        # ``PageRead.source`` is typed ``str`` for ease of construction
        # across callers; ``Citation.source`` narrows to
        # ``Literal["library", "wiki"]`` at the data-shape boundary.
        # The two values are produced from the same closed set
        # (``_build_library_page_read`` → ``"library"``, ``_try_read``
        # → ``"wiki"``), so the cast is safe.
        c = Citation(
            path=page.rel_path,
            source=cast(Literal["library", "wiki"], page.source),
        )
        citations.append(c)
        pages_read.append(c)
        page_links.append(f"[{page.title}]({page.rel_path})")
        excerpt = _excerpt_from_spans(page.spans)
        bullets.append(
            f"- [{page.source}] {page.title} — {excerpt} — [{page.title}]({page.rel_path})"
        )

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

    validated_format = validate_format(answer, format_hint)

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

    The public ``synthesize_answer`` only catches the sentinel
    exceptions above; the real ``lies.qmd.cli`` exception types are
    mapped to them here so the public surface stays narrow and stable.

    ``collection_filter`` (Task 6 / Bundle C) is the resolved set of
    collection names the caller wants to scope to. It is passed through
    to ``fn`` as a keyword argument so any qmd_search callable that
    supports per-collection filtering (notably :func:`lies.qmd.cli.qmd_query`,
    which applies the filter post-qmd as a per-hit path-prefix drop)
    can act on it. Stubs using ``*args, **kwargs`` ignore it; the
    production :func:`qmd_query` honors it.
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

    Library collections are the source of truth for tag expression
    resolution — wikis do not own collections. A ``+c:opencode`` filter
    matches every library collection named ``opencode``, regardless of
    which wiki the operator's MCP daemon is bound to.

    Implicit self-tag (spec: §"Collection name as implicit self-tag"):
    a collection matches if its ``name`` is in ``tags ∪ {name}``. The
    rule lives here at the retriever boundary, not in the resolver —
    the resolver only validates that every atom is in the available
    tag set; the per-collection semantics are the retriever's concern.

    F15 ``t:`` / ``c:`` qualifier dispatch:
            - ``t`` (or no qualifier): the implicit-self-tag rule above.
            - ``c``: strict name match (``coll.name == include.tag``).

    Exclude drops a collection per the same dispatch
    (``_exclude_atom_matches``); regardless of include result, the
    exclude wins on collision (a collection listed by an include and
    the exclude at the same time is dropped).

    Returns the set of *names* (qmd's per-collection filter key, which
    is the path's first segment). Empty when the library is not
    initialized — the operator-facing surfaces (MCP / CLI) detect that
    separately and tell the operator to initialize the library.
    """
    from lies.library.registry import library_collection_metas
    from lies.query.tag_expr import _exclude_atom_matches, atom_matches

    matching: set[str] = set()

    exclude = tag_filter.exclude
    exclude_qualifier = tag_filter.exclude_qualifier
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
        if exclude is not None and _exclude_atom_matches(coll, exclude, exclude_qualifier):
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

    With a filter: the resolved collection set from
    :func:`_collections_matching` (sorted, unique). Without a filter:
    every collection registered in the library per
    :func:`_all_collection_names`. Empty in either case when no
    collections are registered.

    The result is the Bundle C answer-shape contract: the
    orchestrator populates ``SynthesizedAnswer.searched_scope`` from
    this helper so downstream surfaces (F12 elicitation, F16 catalog
    pages_read_by_collection) can react to the effective scope. The
    ``wiki`` parameter is retained for signature uniformity with the
    Bundle C spec contract (``searched_scope(wiki, tag_filter)``); the
    implementation reads the library, not the wiki, because the
    library is the source of truth.
    """
    if tag_filter is not None:
        return sorted(_collections_matching(tag_filter))
    return _all_collection_names()
