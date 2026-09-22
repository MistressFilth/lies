"""Tests for src/lies/agents/librarian.py — F18."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lies.agents.librarian import (
    LibrarianDeps,
    LibrarianOutput,
    PageExcerpt,
    _rewrite_query_for_validator,
)
from lies.library.registry import library_collection_names, library_git_root


def test_rewrite_query_drops_in_word_hyphens() -> None:
    assert _rewrite_query_for_validator("error-handling pattern") == "error handling pattern"
    assert _rewrite_query_for_validator("comma-separated values") == "comma separated values"
    assert _rewrite_query_for_validator("case-insensitive match") == "case insensitive match"


def test_rewrite_query_handles_iso_dates() -> None:
    assert _rewrite_query_for_validator("on 2026-05-10 release") == "on May 10 2026 release"


def test_rewrite_query_passes_through_normal_text() -> None:
    assert _rewrite_query_for_validator("how does auth work?") == "how does auth work?"


def test_librarian_deps_required_fields() -> None:
    deps = LibrarianDeps(
        question="how does pydantic validate nested models?",
        tag_expr="python",
        exclude_tags=["postgres"],
        top_k=5,
    )
    assert deps.top_k == 5
    assert deps.tag_expr == "python"


def test_page_excerpt_carries_spans_not_text_blob() -> None:
    from lies.markdown_spans import Span

    spans = [
        Span(heading_path=["H1"], body="body text", code_fence=False, start_line=1),
    ]
    pe = PageExcerpt(collection="wiki", slug="x", title="X", spans=spans)
    assert pe.spans == spans


def test_librarian_output_distinct_pages_derives_from_excerpts() -> None:
    from lies.markdown_spans import Span

    spans = [Span(heading_path=[], body="b", code_fence=False, start_line=1)]
    out = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[
            PageExcerpt(collection="wiki", slug="a", title="A", spans=spans),
            PageExcerpt(collection="wiki", slug="a", title="A", spans=spans),
            PageExcerpt(collection="wiki", slug="b", title="B", spans=spans),
        ],
        distinct_pages=2,  # caller computes; test the field is settable
    )
    assert out.distinct_pages == 2


def test_librarian_output_defaults_no_coverage_false() -> None:
    """F18 no_coverage field defaults to False (back-compat)."""
    out = LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=[], distinct_pages=0)
    assert out.no_coverage is False


def test_librarian_output_no_coverage_settable() -> None:
    """F18 no_coverage field is settable to True via keyword."""
    out = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[],
        distinct_pages=0,
        no_coverage=True,
    )
    assert out.no_coverage is True


def test_register_librarian_tools_captures_no_coverage_from_wiki_search(
    empty_wiki: object,
) -> None:
    """wiki_search closure captures result's no_coverage flag into the ContextVar.

    Pins F18 Task 1: ``register_librarian_tools``'s ``_wiki_search``
    closure must thread the search result's ``no_coverage`` flag into
    the module-scope ``librarian_no_coverage`` ContextVar so the
    orchestrator's dispatch site can copy it onto the returned
    ``LibrarianOutput``. Drives the registered tool directly via
    pydantic-ai's toolset API — the closure is private to
    ``register_librarian_tools`` so we exercise it through the
    agent's public tool registry instead.
    """
    from lies.agents import librarian as librarian_mod
    from pydantic_ai import RunContext
    from pydantic_ai.models.test import TestModel

    # Reset to default so the assertion below is independent of
    # any prior test leaving the ContextVar set.
    librarian_mod.librarian_no_coverage.set(False)

    class _FakeSearchResult:
        def model_dump(self) -> dict[str, object]:
            return {"hits": [], "no_coverage": True, "searched_scope": []}

    class _FakeMemoryService:
        def search(self, question: str, *, limit: int = 5) -> _FakeSearchResult:
            return _FakeSearchResult()

    agent = librarian_mod.librarian_agent(model="test")
    librarian_mod.register_librarian_tools(
        agent,
        wiki=empty_wiki,  # type: ignore[arg-type]
        memory_service=_FakeMemoryService(),  # type: ignore[arg-type]
    )

    # Pull the registered ``wiki_search`` closure out of the toolset
    # and invoke it with a stub ``RunContext`` so we exercise the
    # capture path end-to-end without spinning up the LLM.
    tool_fn: object | None = None
    for toolset in agent.toolsets:
        tool = toolset.tools.get("wiki_search")
        if tool is not None:
            tool_fn = tool.function
            break
    assert tool_fn is not None, "wiki_search tool not registered"

    deps = LibrarianDeps(question="q", tag_expr=None, exclude_tags=[], top_k=5)
    ctx = RunContext(
        model=TestModel(),
        deps=deps,
        usage=None,  # type: ignore[arg-type]
    )
    result = tool_fn(ctx, "q", 5)  # type: ignore[misc]

    assert result == {"hits": [], "no_coverage": True, "searched_scope": []}
    assert librarian_mod.librarian_no_coverage.get() is True


# ---------------------------------------------------------------------------
# Task 3 — dual-source retrieval (wiki + library); library-wins-on-conflict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeWikiEvidence:
    """Minimal WikiEvidence-shape stand-in for the librarian tests.

    Mirrors the four fields ``_wiki_search`` reads from each wiki hit
    (``page_id``, ``path``, ``collection_id``, ``excerpt``) plus the
    score used for ranking. The real :class:`WikiEvidence` is a
    pydantic model; a frozen dataclass with matching attribute names
    satisfies duck-typing without dragging the full memory chain.
    """

    page_id: str
    path: str
    collection_id: str
    excerpt: str
    score: float = 0.5
    line_start: int = 1
    line_end: int = 1


@dataclass(frozen=True)
class _FakeWikiSearchResult:
    """Wiki-side search result carrying pages + no_coverage + searched_scope."""

    pages: list[_FakeWikiEvidence]
    no_coverage: bool = False
    searched_scope: tuple[str, ...] = ()

    def model_dump(self) -> dict[str, Any]:
        return {
            "pages": [page.__dict__ for page in self.pages],
            "no_coverage": self.no_coverage,
            "searched_scope": list(self.searched_scope),
        }


@dataclass(frozen=True)
class _FakeMemoryService:
    """Wiki-side memory service that returns a canned search result."""

    pages: list[_FakeWikiEvidence]
    no_coverage: bool = False
    searched_scope: tuple[str, ...] = ()

    def search(self, question: str, *, limit: int = 5) -> _FakeWikiSearchResult:
        return _FakeWikiSearchResult(
            pages=self.pages,
            no_coverage=self.no_coverage,
            searched_scope=self.searched_scope,
        )


@dataclass(frozen=True)
class _FakeWiki:
    """Minimal Wiki stand-in — exposes only the ``wiki_dir`` attribute."""

    wiki_dir: Path


def _drive_wiki_search(
    *,
    memory_service: _FakeMemoryService,
    qmd_query_fn: Any,
    wiki: _FakeWiki | None = None,
) -> dict[str, object]:
    """Build a librarian agent with dual-source wiring and invoke ``wiki_search``.

    Mirrors the registered-tool exercise in
    :func:`test_register_librarian_tools_captures_no_coverage_from_wiki_search`
    so the new dual-source tests don't depend on the LLM stack — they
    drive the registered ``wiki_search`` closure directly.
    """
    from lies.agents import librarian as librarian_mod
    from pydantic_ai import RunContext
    from pydantic_ai.models.test import TestModel

    agent = librarian_mod.librarian_agent(model="test")
    librarian_mod.register_librarian_tools(
        agent,
        wiki=wiki if wiki is not None else _FakeWiki(wiki_dir=Path("/tmp/fake-wiki")),  # type: ignore[arg-type]
        memory_service=memory_service,  # type: ignore[arg-type]
        qmd_query=qmd_query_fn,
    )

    tool_fn: object | None = None
    for toolset in agent.toolsets:
        tool = toolset.tools.get("wiki_search")
        if tool is not None:
            tool_fn = tool.function
            break
    assert tool_fn is not None, "wiki_search tool not registered"

    deps = LibrarianDeps(question="q", tag_expr=None, exclude_tags=[], top_k=5)
    ctx = RunContext(
        model=TestModel(),
        deps=deps,
        usage=None,  # type: ignore[arg-type]
    )
    return tool_fn(ctx, "q", 5)  # type: ignore[misc]


def test_wiki_search_returns_library_hits_with_source_kind_library(monkeypatch) -> None:
    """_wiki_search surfaces library hits tagged source_kind='library'.

    Both sides are now qmd-driven (Task 1): the wiki-side qmd call
    is the wiki hit source, and the library-side qmd call is the
    library hit source. The fake returns the library hit regardless
    of cwd; library-wins-on-conflict means the merged row carries
    the library body's title/excerpt.
    """
    # Wiki side empty (no qmd hits); library side returns one hit.
    wiki_service = _FakeMemoryService(pages=[], no_coverage=False)
    qmd_calls: list[dict[str, Any]] = []

    def fake_qmd_query(cwd: Path, q: str, limit: int = 5, **_kw: Any) -> list[dict[str, Any]]:
        qmd_calls.append({"cwd": cwd, "q": q, "limit": limit})
        return [{"path": "concepts/pydantic", "title": "Pydantic concept", "score": 0.8}]

    out = _drive_wiki_search(memory_service=wiki_service, qmd_query_fn=fake_qmd_query)

    assert qmd_calls, "qmd library path must be queried"
    hits = out["hits"]
    assert len(hits) == 1
    assert hits[0]["source_kind"] == "library"
    assert hits[0]["path"] == "concepts/pydantic"


def test_wiki_search_returns_wiki_only_hits_with_source_kind_wiki(monkeypatch) -> None:
    """_wiki_search surfaces wiki-only hits tagged source_kind='wiki'.

    The wiki-side qmd call (cwd == wiki.wiki_dir) returns the wiki
    hit; the library-side qmd call (cwd == lib.git_root) returns
    nothing. The merged row carries the wiki hit and is tagged
    ``source_kind="wiki"``.
    """
    from lies.library.registry import library_git_root

    wiki_service = _FakeMemoryService(pages=[], no_coverage=False)
    fake_wiki = _FakeWiki(wiki_dir=Path("/tmp/fake-wiki"))
    lib_root = library_git_root()
    wiki_hit = {
        "path": "concepts/pydantic",
        "title": "Wiki pydantic",
        "score": 0.7,
        "excerpt": "Wiki excerpt about pydantic",
    }

    def fake_qmd_query(cwd: Path, q: str, limit: int = 5, **_kw: Any) -> list[dict[str, Any]]:
        if Path(cwd) == fake_wiki.wiki_dir:
            return [wiki_hit]
        if Path(cwd) == lib_root:
            return []
        return []

    out = _drive_wiki_search(
        wiki=fake_wiki,
        memory_service=wiki_service,
        qmd_query_fn=fake_qmd_query,
    )

    hits = out["hits"]
    assert len(hits) == 1
    assert hits[0]["source_kind"] == "wiki"
    assert hits[0]["path"] == "concepts/pydantic"


def test_wiki_search_library_wins_on_slug_conflict(monkeypatch) -> None:
    """When wiki and library both hit the same slug, the library hit wins.

    The wiki-side qmd call returns the wiki hit; the library-side
    qmd call returns the library hit. Both carry the same ``path``;
    the merge drops the wiki copy on slug match and keeps the
    library row tagged ``source_kind="library"``.
    """
    from lies.library.registry import library_git_root

    wiki_service = _FakeMemoryService(pages=[], no_coverage=False)
    fake_wiki = _FakeWiki(wiki_dir=Path("/tmp/fake-wiki"))
    lib_root = library_git_root()
    wiki_hit = {
        "path": "concepts/pydantic",
        "title": "Pydantic (wiki)",
        "score": 0.7,
        "excerpt": "Wiki excerpt about pydantic",
    }
    library_hit = {
        "path": "concepts/pydantic",
        "title": "Pydantic (library)",
        "score": 0.9,
        "excerpt": "Library excerpt about pydantic",
    }

    def fake_qmd_query(cwd: Path, q: str, limit: int = 5, **_kw: Any) -> list[dict[str, Any]]:
        if Path(cwd) == fake_wiki.wiki_dir:
            return [wiki_hit]
        if Path(cwd) == lib_root:
            return [library_hit]
        return []

    out = _drive_wiki_search(
        wiki=fake_wiki,
        memory_service=wiki_service,
        qmd_query_fn=fake_qmd_query,
    )

    hits = out["hits"]
    assert len(hits) == 1
    hit = hits[0]
    # Library wins on conflict — the merged row carries the library
    # title/excerpt and is tagged as a library hit.
    assert hit["source_kind"] == "library"
    assert hit["path"] == "concepts/pydantic"
    assert hit["title"] == "Pydantic (library)"
    assert hit["excerpt"] == "Library excerpt about pydantic"


# ---------------------------------------------------------------------------
# Task 1 — librarian qmd fan-out hits both wiki.wiki_dir AND lib.git_root
# ---------------------------------------------------------------------------


def test_wiki_search_queries_library_git_root() -> None:
    """Librarian fan-out hits both wiki.wiki_dir and lib.git_root.

    Pre-fix bug: qmd_query was called only against wiki.wiki_dir with
    a library-collection filter, so library hits were always empty
    (the wiki's qmd index has no library pages registered). The
    library's qmd index is registered at lib.git_root
    (``~/.local/share/lies/library/``), NOT at any wiki's
    ``wiki_dir``. The fix splits the fan-out so qmd_query is called
    against both surfaces in parallel; both feed
    ``_merge_wiki_and_library_hits`` which enforces
    library-wins-on-slug-conflict.

    Reads the live ``wiki.wiki_dir`` and ``library_git_root()`` paths
    to build the expected cwd list so the assertion tracks the
    actual layout, not hard-coded sentinels.
    """
    wiki_service = _FakeMemoryService(pages=[], no_coverage=False)
    fake_wiki = _FakeWiki(wiki_dir=Path("/tmp/fake-wiki"))
    expected_lib_root = library_git_root()
    called_cwds: list[Path] = []

    def fake_qmd_query(cwd: Path, q: str, limit: int = 5, **_kw: Any) -> list[dict[str, Any]]:
        called_cwds.append(Path(cwd))
        return []

    _drive_wiki_search(
        wiki=fake_wiki,
        memory_service=wiki_service,
        qmd_query_fn=fake_qmd_query,
    )

    assert called_cwds == [fake_wiki.wiki_dir, expected_lib_root], (
        f"librarian must fan out to both wiki cwd and library git root; got {called_cwds}"
    )


def test_wiki_search_passes_collection_filter_to_library_side_only() -> None:
    """Wiki-side qmd_query has no filter; library-side carries the collection_filter.

    Confirms the fan-out semantics the fix introduces: only the
    library-side call needs ``collection_filter=set(library_collection_names())``
    because qmd's collection filter restricts results to the named
    library collections. The wiki-side call indexes wiki pages, not
    library pages, so applying the filter there would drop every
    wiki hit on the (correct) theory that no wiki slug matches a
    library-collection name.
    """
    wiki_service = _FakeMemoryService(pages=[], no_coverage=False)
    fake_wiki = _FakeWiki(wiki_dir=Path("/tmp/fake-wiki"))
    expected_lib_root = library_git_root()
    expected_lib_filter = set(library_collection_names())
    call_log: list[dict[str, Any]] = []

    def fake_qmd_query(cwd: Path, q: str, limit: int = 5, **kw: Any) -> list[dict[str, Any]]:
        call_log.append({"cwd": Path(cwd), "collection_filter": kw.get("collection_filter")})
        return []

    _drive_wiki_search(
        wiki=fake_wiki,
        memory_service=wiki_service,
        qmd_query_fn=fake_qmd_query,
    )

    assert [c["cwd"] for c in call_log] == [fake_wiki.wiki_dir, expected_lib_root]
    assert call_log[0]["collection_filter"] is None, "wiki side must not pass a filter"
    assert call_log[1]["collection_filter"] == expected_lib_filter, (
        "library side must pass collection_filter=set(library_collection_names())"
    )
