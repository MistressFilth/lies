"""Wiki search and page reading.

Search prefers qmd and falls back to ``wiki/index.md`` when qmd is
unavailable, returns no results, or fails. Page reads accept the
``page_id`` values produced by a prior search, never model-supplied
filesystem paths.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path

from lies.memory.models import (
    WikiEvidence,
    WikiSearchResult,
)
from lies.memory.validation import validate_page_path
from lies.qmd.cli import (
    QmdCommandError,
    QmdNoResultsError,
    QmdNotInstalledError,
)
from lies.query.index_parser import parse_index_links
from lies.wiki.wiki import Wiki

_QMD_FALLBACK_UNAVAILABLE = "qmd_unavailable"
_QMD_FALLBACK_NO_RESULTS = "qmd_no_results"
_QMD_FALLBACK_FAILED = "qmd_failed"


def _page_id_for(path: str) -> str:
    """Return a stable id for ``path``."""
    return "page-" + hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


def _excerpt(content: str, *, max_chars: int = 320) -> str:
    """Return the first non-empty paragraph from a page body."""
    text = re.sub(r"^---\n.*?\n---\n", "", content, count=1, flags=re.DOTALL)
    for paragraph in text.split("\n\n"):
        cleaned = paragraph.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:max_chars]
    return ""


def _line_range_for_excerpt(content: str, excerpt: str) -> tuple[int, int]:
    text = re.sub(r"^---\n.*?\n---\n", "", content, count=1, flags=re.DOTALL)
    lines = text.splitlines()
    if not excerpt:
        return (0, 0)
    head = excerpt.splitlines()[0].strip()[:40]
    for idx, line in enumerate(lines):
        if head and head in line:
            return (idx, idx + max(len(excerpt.splitlines()), 1))
    return (0, max(len(lines) - 1, 0))


def _read_page_content(wiki: Wiki, path: str) -> str:
    resolved = validate_page_path(wiki, path)
    if not resolved.exists():
        return ""
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _from_qmd(
    wiki: Wiki,
    question: str,
    limit: int,
    qmd_search: Callable[..., list[dict[str, object]]],
    *,
    collection_filter: set[str] | None = None,
) -> tuple[list[WikiEvidence], bool, str]:
    try:
        results = qmd_search(
            wiki.data_root, question, limit + 1, collection_filter=collection_filter
        )
    except QmdNotInstalledError:
        return ([], True, _QMD_FALLBACK_UNAVAILABLE)
    except QmdNoResultsError:
        return ([], True, _QMD_FALLBACK_NO_RESULTS)
    except QmdCommandError:
        return ([], True, _QMD_FALLBACK_FAILED)

    evidences: list[WikiEvidence] = []
    for item in results[: limit + 1]:
        raw_path = str(item.get("path", ""))
        if not raw_path:
            continue
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (wiki.wiki_dir / raw_path).resolve()
        # Compute the path relative to ``wiki_dir`` so it matches the
        # convention used by ``validate_page_path`` and the index parser.
        try:
            wiki_rel = candidate.relative_to(wiki.wiki_dir).as_posix()
        except ValueError:
            continue
        try:
            validate_page_path(wiki, wiki_rel)
        except Exception:  # noqa: BLE001, S112 - rejection is the point
            continue
        # Phantom-path gate: every well-formed wiki-relative path must
        # resolve to a real file before we mint a page_id. The wiki
        # catalog can hold a qmd index for a library collection that is
        # not actually materialized under ``wiki.wiki_dir``; without this
        # gate ``_from_qmd`` mints ``page-<sha1>[:12]`` ids for files
        # that never existed, and the downstream ``wiki_read`` raises
        # ``WikiPageNotFound``. Repro of the 2026-09-22 transcript bug:
        # ``opencode/config.md`` (a library path) → ``page-2da7bf8c551d``.
        resolved_file = wiki.wiki_dir / wiki_rel
        if not resolved_file.is_file():
            continue
        score_raw = item.get("score", 0.0)
        if isinstance(score_raw, (int, float)):
            score = float(score_raw)
        else:
            score = 0.0
        content = _read_page_content(wiki, wiki_rel)
        excerpt = _excerpt(content)
        start, end = _line_range_for_excerpt(content, excerpt)
        evidences.append(
            WikiEvidence(
                page_id=_page_id_for(wiki_rel),
                path=wiki_rel,
                collection_id=wiki.name,
                excerpt=excerpt,
                line_start=start,
                line_end=end,
                score=score,
            )
        )
    if not evidences:
        return ([], True, _QMD_FALLBACK_NO_RESULTS)
    truncated = len(evidences) > limit
    return (evidences[:limit], truncated, "")


def _from_index(wiki: Wiki, question: str, limit: int) -> list[WikiEvidence]:
    index_path = wiki.wiki_dir / "index.md"
    if not index_path.exists():
        return []
    content = index_path.read_text(encoding="utf-8")
    links = parse_index_links(content)
    query_terms = {token.lower() for token in re.findall(r"\w+", question)}
    evidences: list[WikiEvidence] = []
    for link in links:
        body = _read_page_content(wiki, link.path)
        if not body:
            continue
        haystack = (link.title + " " + body).lower()
        if query_terms and not any(term in haystack for term in query_terms):
            continue
        excerpt = _excerpt(body)
        start, end = _line_range_for_excerpt(body, excerpt)
        evidences.append(
            WikiEvidence(
                page_id=_page_id_for(link.path),
                path=link.path,
                collection_id=wiki.name,
                excerpt=excerpt,
                line_start=start,
                line_end=end,
                score=0.5,
            )
        )
        if len(evidences) >= limit:
            break
    return evidences


def _corpus_page_count(wiki: Wiki) -> int:
    """Return the wiki-section page count for ``wiki``.

    Probes ``<wiki_dir>/.lies/catalog.db`` for ``section='wiki'``
    rows. The wiki section is the right corpus for the search
    no-coverage flag — library mirrors live under ``section='ingested'``
    and are not the wiki corpus. Best-effort: any exception (wiki
    unresolvable, catalog unreadable, missing ``wiki_dir``) propagates
    and the caller falls back to ``no_coverage=False``.

    Module-scope so tests can monkeypatch it without touching the
    catalog. Mirrors :func:`lies.mcp.grounding._corpus_page_count`,
    which serves the F19 ground-shape digest — keeping both paths off
    one helper preserves the contract across surfaces.
    """
    from lies.memory.catalog import list_pages, open_catalog
    from lies.memory.catalog_models import PageSection

    conn = open_catalog(wiki)
    try:
        return len(list_pages(conn, section=PageSection.wiki))
    finally:
        conn.close()


def _no_coverage_flag(wiki: Wiki, hits: list[WikiEvidence]) -> bool:
    """Compute the ``no_coverage`` flag for a search result.

    ``True`` when ``hits`` is empty AND the wiki corpus has at least
    one page. ``False`` when the corpus is empty (an unpopulated wiki
    is not a no-coverage signal) or when the search returned hits (a
    successful query is never a no-coverage signal regardless of
    corpus size). A catalog probe failure degrades to ``False`` —
    fail-open rather than reporting a false positive.
    """
    if hits:
        return False
    try:
        return _corpus_page_count(wiki) > 0
    except Exception:  # noqa: BLE001 - fail-open: missed probes map to False
        return False


def search_wiki(
    wiki: Wiki,
    question: str,
    *,
    limit: int = 5,
    qmd_search: Callable[..., list[dict[str, object]]] | None = None,
    collection_filter: set[str] | None = None,
) -> WikiSearchResult:
    """Search the wiki and return bounded evidence.

    ``collection_filter`` (F18 Task 6 / Bundle C) threads the resolved
    tag-expression set down to qmd. qmd applies its post-filter
    (``qmd://<coll>/<rest>`` first-segment match) before the hits
    ever leave the CLI, so library collections the operator excluded
    never produce phantom wiki-side paths. ``None`` keeps the
    untagged behavior unchanged for every existing caller.
    """
    if qmd_search is None:
        # Resolve ``qmd_query`` at call time so monkeypatching
        # ``lies.qmd.cli.qmd_query`` takes effect.
        from lies import qmd as _qmd_module

        qmd_search = _qmd_module.cli.qmd_query
    if not question or not question.strip():
        return WikiSearchResult(
            query=question or "",
            pages=[],
            truncated=False,
            fallback_used=True,
            fallback_reason="empty_query",
            no_coverage=_no_coverage_flag(wiki, []),
        )
    evidences, truncated, fallback_reason = _from_qmd(
        wiki, question, limit, qmd_search, collection_filter=collection_filter
    )
    if not evidences:
        evidences = _from_index(wiki, question, limit)
    return WikiSearchResult(
        query=question,
        pages=evidences,
        truncated=truncated,
        fallback_used=bool(fallback_reason),
        fallback_reason=fallback_reason,
        no_coverage=_no_coverage_flag(wiki, evidences),
    )


def read_pages(wiki: Wiki, page_ids: list[str]) -> dict[str, str]:
    """Read full page markdown for the given page IDs.

    Page IDs are the values returned by :func:`search_wiki`. Unknown
    IDs are silently skipped (the returned dict omits them). The read
    result always reflects the on-disk state, not a cached copy.
    """
    if not page_ids:
        return {}
    bodies: dict[str, str] = {}
    for page_id in page_ids:
        # The page id encodes the path; we recompute the id->path map
        # by scanning the wiki for the matching id. This keeps the
        # service free of model-supplied filesystem paths.
        path = _path_for_id(wiki, page_id)
        if path is None:
            continue
        bodies[page_id] = _read_page_content(wiki, path)
    return bodies


def _path_for_id(wiki: Wiki, page_id: str) -> str | None:
    for path in sorted(wiki.wiki_dir.rglob("*.md")):
        rel = path.relative_to(wiki.wiki_dir).as_posix()
        if rel in {"index.md", "log.md", "overview.md", "lint-report.md"}:
            continue
        if _page_id_for(rel) == page_id:
            return rel
    return None
