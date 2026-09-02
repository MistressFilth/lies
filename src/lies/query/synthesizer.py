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

from lies.qmd.cli import qmd_query
from lies.query.index_parser import parse_index_links
from lies.query.models import SynthesizedAnswer
from lies.wiki.wiki import Wiki

DEFAULT_TOP_N = 5

# Reasons the fallback was triggered (also see SynthesizedAnswer docs).
FALLBACK_REASON_UNAVAILABLE = "qmd_unavailable"
FALLBACK_REASON_NO_RESULTS = "qmd_no_results"
FALLBACK_REASON_FAILED = "qmd_failed"

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
    """A page read during retrieval."""

    rel_path: str  # wiki-relative, POSIX
    title: str
    excerpt: str


def retrieve_pages(
    question: str,
    wiki: Wiki,
    *,
    top_n: int = DEFAULT_TOP_N,
    qmd_search: QmdSearchFn | None = None,
) -> tuple[list[PageRead], str]:
    """Retrieve the candidate pages for ``question``.

    Tries ``qmd_search`` first; on any qmd failure falls back to the
    top-N pages referenced by ``wiki/index.md``.

    This is the single retrieval path for the query layer. Both the
    extractive ``synthesize_answer`` and the orchestrator's LLM
    synthesis consume it, so the agent and its extractive fallback can
    never disagree about which pages were read.

    ``qmd_search`` defaults to the module-level indirection over
    ``lies.qmd.cli.qmd_query`` (rebindable via :func:`set_qmd_search`),
    not a captured default argument. Tests stub the indirection; the
    indirection is looked up at call time so a stub rebind actually
    takes effect for subsequent calls.

    Returns:
        ``(pages, fallback_reason)``. ``fallback_reason`` is ``""``
        when qmd served the query, else one of the ``FALLBACK_REASON_*``
        constants. ``pages`` may be empty when nothing was readable.
    """
    pages: list[PageRead] = []
    fallback_reason = ""

    qmd_search_fn = qmd_search if qmd_search is not None else _qmd_search_default()
    try:
        pages = _qmd_search_dispatch(qmd_search_fn, wiki, question, top_n)
    except _QmdUnavailable:
        fallback_reason = FALLBACK_REASON_UNAVAILABLE
    except _QmdNoResults:
        fallback_reason = FALLBACK_REASON_NO_RESULTS
    except _QmdOtherFailure:
        fallback_reason = FALLBACK_REASON_FAILED

    if fallback_reason:
        pages = _read_pages_from_index(wiki, top_n=top_n)

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
    order (which the indexer keeps alphabetical within each section).
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


def _resolve_qmd_pages(wiki: Wiki, qmd_paths: list[str], top_n: int) -> list[PageRead]:
    """Resolve qmd-returned paths to actual readable pages on disk.

    Defends against path traversal: any returned path that escapes
    ``wiki.data_root`` is silently dropped.
    """
    pages: list[PageRead] = []
    for raw in qmd_paths:
        if len(pages) >= top_n:
            break
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (wiki.wiki_dir / raw).resolve()
        try:
            candidate.relative_to(wiki.wiki_dir)
        except ValueError:
            continue
        read = _try_read(candidate, wiki)
        if read is not None:
            pages.append(read)
    return pages


def _try_read(path: Path, wiki: Wiki, *, title_override: str | None = None) -> PageRead | None:
    """Read a page; return None if missing/unreadable."""
    if not path.exists() or not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    rel = path.relative_to(wiki.data_root).as_posix()
    title = title_override or _extract_title(content) or path.stem
    excerpt = _first_meaningful_paragraph(content)
    return PageRead(rel_path=rel, title=title, excerpt=excerpt)


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


def _first_meaningful_paragraph(content: str, max_chars: int = 400) -> str:
    """Return the first non-heading, non-empty paragraph, truncated.

    Skips YAML frontmatter and headings. Concatenates consecutive
    non-empty lines into a single paragraph, up to ``max_chars``.
    """
    in_fm = False
    seen_fm = False
    para: list[str] = []
    for line in content.splitlines():
        if not seen_fm and line.strip() == "---":
            in_fm = not in_fm
            if not in_fm:
                seen_fm = True
            continue
        if in_fm:
            continue
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if stripped.startswith("#"):
            if para:
                break
            continue
        para.append(stripped)
        if len(" ".join(para)) >= max_chars:
            break
    text = " ".join(para).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text


# ---------------------------------------------------------------------------
# Answer assembly
# ---------------------------------------------------------------------------


def build_answer_from_pages(
    question: str, pages: list[PageRead], fallback_reason: str
) -> SynthesizedAnswer:
    """Assemble the final SynthesizedAnswer from already-retrieved ``pages``.

    Public seam for the extractive answer builder: callers (notably
    :meth:`lies.orchestrator.Orchestrator.run_query`) that have already
    called :func:`retrieve_pages` once pass the resulting ``pages``
    through here so the fallback path doesn't pay for a second qmd /
    index scan. ``pages`` may be empty, in which case the returned
    answer has empty citations / pages_read / page_links and a body
    describing why.

    Args:
        question: The user's natural-language question.
        pages: The pages already retrieved for ``question``. Empty is
            valid; the returned body explains what was missing.
        fallback_reason: One of the ``FALLBACK_REASON_*`` constants;
            empty when qmd served the query.

    Returns:
        A :class:`SynthesizedAnswer` whose ``fallback_used`` /
        ``fallback_reason`` mirror the inputs. ``synthesis_used`` is
        always False here — the orchestrator wraps this with the
        synthesis metadata (``synthesis_used``, ``synthesis_reason``)
        since this function has no opinion on whether the LLM was
        invoked.
    """
    citations: list[str] = []
    pages_read: list[str] = []
    page_links: list[str] = []
    bullets: list[str] = []
    for page in pages:
        citations.append(page.rel_path)
        pages_read.append(page.rel_path)
        page_links.append(f"[{page.title}]({page.rel_path})")
        excerpt = page.excerpt or "(no extractable content)"
        bullets.append(f"- {page.title} — {excerpt} — [{page.title}]({page.rel_path})")

    if fallback_reason:
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

    return SynthesizedAnswer(
        answer=answer,
        citations=citations,
        pages_read=pages_read,
        fallback_used=bool(fallback_reason),
        fallback_reason=fallback_reason,
        page_links=page_links,
    )


def _empty_answer(question: str, fallback_reason: str) -> str:
    """The 'no pages found' answer body."""
    if fallback_reason == FALLBACK_REASON_NO_RESULTS:
        return (
            f"### {question.strip()}\n\n"
            "_qmd query returned no results, and `wiki/index.md` "
            "contains no readable pages._\n\n"
            "No pages found."
        )
    if fallback_reason == FALLBACK_REASON_UNAVAILABLE:
        return (
            f"### {question.strip()}\n\n"
            "_qmd is not installed, and `wiki/index.md` contains no "
            "readable pages._\n\n"
            "No pages found."
        )
    if fallback_reason == FALLBACK_REASON_FAILED:
        return (
            f"### {question.strip()}\n\n"
            "_qmd query failed, and `wiki/index.md` contains no "
            "readable pages._\n\n"
            "No pages found."
        )
    return f"### {question.strip()}\n\nNo pages found."


# ---------------------------------------------------------------------------
# Qmd dispatch: translate real qmd exceptions into sentinels
# ---------------------------------------------------------------------------


def _qmd_search_dispatch(fn: QmdSearchFn, wiki: Wiki, question: str, top_n: int) -> list[PageRead]:
    """Call ``fn`` and translate its real exceptions into sentinels.

    The public ``synthesize_answer`` only catches the sentinel
    exceptions above; the real ``lies.qmd.cli`` exception types are
    mapped to them here so the public surface stays narrow and stable.
    """
    # Local imports avoid a circular import at module load time.
    from lies.qmd.cli import (  # noqa: WPS433
        QmdCommandError,
        QmdNoResultsError,
        QmdNotInstalledError,
    )

    try:
        results = fn(wiki.data_root, question, top_n)
    except QmdNotInstalledError as exc:
        raise _QmdUnavailable(str(exc)) from exc
    except QmdNoResultsError as exc:
        raise _QmdNoResults(str(exc)) from exc
    except QmdCommandError as exc:
        raise _QmdOtherFailure(str(exc)) from exc

    qmd_paths = [
        path for r in results if isinstance(r, dict) and isinstance((path := r.get("path")), str)
    ]
    pages = _resolve_qmd_pages(wiki, qmd_paths, top_n)
    if not pages:
        # qmd gave us hits but none of the files are readable — treat as
        # "no results" so the fallback path runs.
        raise _QmdNoResults("qmd returned no readable pages")
    return pages
