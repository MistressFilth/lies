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

    # Pass ``qmd_query`` explicitly as an empty stub so the librarian's
    # library-side merge is a no-op. The default ``qmd_query`` helper
    # lazy-imports ``lies.qmd.cli`` on first call and shells out to the
    # qmd binary against ``wiki.wiki_dir`` — a real subprocess in a unit
    # test, and the dominant cost of this regression pin (~30s).
    agent = librarian_mod.librarian_agent(model="test")
    librarian_mod.register_librarian_tools(
        agent,
        wiki=empty_wiki,  # type: ignore[arg-type]
        memory_service=_FakeMemoryService(),  # type: ignore[arg-type]
        qmd_query=lambda *_a, **_kw: [],
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


def test_wiki_search_maps_qmd_docid_to_wiki_page_id(monkeypatch) -> None:
    """Wiki-side qmd hits must carry the wiki page- + sha1-12 ID.

    Pre-fix bug: the wiki-side qmd query returns ``page_id='#abc123'``
    (qmd docid format). ``_wiki_read`` only recognizes wiki
    ``page-<sha1-12>`` IDs and library paths (``<collection>/<page>``);
    a qmd docid like ``#abc123`` matches neither prefix nor separator,
    so ``_wiki_read(['#d75430'])`` raises ``WikiPageNotFound``. A
    live debug session
    (``5b6fa1e5-75b0-4fe6-84cd-c60ff7fc7ff0``) hit this failure.

    The fix: build a ``path -> page_id`` lookup from
    ``memory_service.search()``'s real wiki hits, then for each
    wiki-side qmd hit replace the qmd docid with the matching
    wiki page_id (or ``None`` if no wiki search hit covers that
    path - best-effort, never raises).
    """
    from lies.library.registry import library_git_root

    wiki_pages = [
        _FakeWikiEvidence(
            page_id="page-2da7bf8c551d",
            path="concepts/pydantic",
            collection_id="wiki",
            excerpt="Wiki excerpt about pydantic",
        ),
    ]
    wiki_service = _FakeMemoryService(pages=wiki_pages, no_coverage=False)
    fake_wiki = _FakeWiki(wiki_dir=Path("/tmp/fake-wiki"))
    lib_root = library_git_root()
    wiki_qmd_hit = {
        "page_id": "#d75430",  # qmd docid, NOT a wiki page_id
        "path": "concepts/pydantic",
        "title": "Wiki pydantic",
        "score": 0.7,
        "excerpt": "qmd excerpt about pydantic",
    }

    def fake_qmd_query(cwd: Path, q: str, limit: int = 5, **_kw: Any) -> list[dict[str, Any]]:
        if Path(cwd) == fake_wiki.wiki_dir:
            return [wiki_qmd_hit]
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
    hit = hits[0]
    assert hit["source_kind"] == "wiki"
    assert hit["path"] == "concepts/pydantic"
    # The qmd docid must be REPLACED by the wiki page- + sha1-12 ID
    # derived from memory_service.search()'s matching wiki page.
    assert hit["page_id"] == "page-2da7bf8c551d"
    # The other wiki-hit fields survive the mapping.
    assert hit["title"] == "Wiki pydantic"
    assert hit["excerpt"] == "qmd excerpt about pydantic"


def test_wiki_search_unmatched_qmd_hit_has_none_page_id(monkeypatch) -> None:
    """A wiki qmd hit whose path doesn't match any wiki search hit gets page_id=None.

    Best-effort fallback (library-style contract): the conversion
    is best-effort - if qmd's hit covers a path the memory-service
    search didn't surface, we set ``page_id=None`` so the LLM agent
    skips ``wiki_read`` rather than calling it with a qmd docid
    that would raise ``WikiPageNotFound``. This mirrors how library
    hits already carry ``page_id=None``.
    """
    from lies.library.registry import library_git_root

    wiki_service = _FakeMemoryService(pages=[], no_coverage=False)
    fake_wiki = _FakeWiki(wiki_dir=Path("/tmp/fake-wiki"))
    lib_root = library_git_root()
    wiki_qmd_hit = {
        "page_id": "#d75430",
        "path": "concepts/orphan",
        "title": "Orphan",
        "score": 0.7,
        "excerpt": "orphan excerpt",
    }

    def fake_qmd_query(cwd: Path, q: str, limit: int = 5, **_kw: Any) -> list[dict[str, Any]]:
        if Path(cwd) == fake_wiki.wiki_dir:
            return [wiki_qmd_hit]
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
    hit = hits[0]
    assert hit["source_kind"] == "wiki"
    # No matching wiki search hit -> page_id=None (library-style fallback).
    assert hit["page_id"] is None


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


# ---------------------------------------------------------------------------
# Task B — strip page_id from library hits; source-aware _wiki_read dispatch
# ---------------------------------------------------------------------------


def test_library_hit_page_id_is_none() -> None:
    """Library hits must carry page_id=None so wiki_read is not called.

    Pre-fix bug (Task B): library hits came from qmd with
    ``#abc123``-style page_id values; the librarian's LLM agent
    called ``wiki_read(['#abc123'])`` which raised WikiPageNotFound
    because ``memory_service.read`` only knows wiki ``page-`` + sha1-12
    IDs. The fix strips qmd's docid from library hits so the LLM
    skips the read step entirely on library hits.

    Drives ``_wiki_search`` end-to-end via the registered tool
    closure (same pattern as the existing dual-source tests) so the
    assertion exercises the actual library-hit-stripping code path,
    not a copy of the strip logic in the test.
    """
    from lies.library.registry import library_git_root

    wiki_service = _FakeMemoryService(pages=[], no_coverage=False)
    qmd_calls: list[dict[str, Any]] = []

    def fake_qmd_query(cwd: Path, q: str, limit: int = 5, **_kw: Any) -> list[dict[str, Any]]:
        qmd_calls.append({"cwd": cwd, "q": q, "limit": limit})
        # Library side returns one hit with a qmd-style docid
        # page_id that the pre-fix code would leak through. Wiki
        # side returns nothing.
        if Path(cwd) == library_git_root():
            return [
                {
                    "page_id": "#d75430",
                    "path": "opencode/config.md",
                    "title": "Config",
                    "excerpt": "library excerpt",
                }
            ]
        return []

    out = _drive_wiki_search(memory_service=wiki_service, qmd_query_fn=fake_qmd_query)

    assert qmd_calls, "qmd library path must be queried"
    hits = out["hits"]
    assert len(hits) == 1
    hit = hits[0]
    # The qmd-style page_id must be stripped; the hit must carry
    # ``page_id=None`` so the LLM agent never tries
    # ``wiki_read(['#d75430'])``.
    assert hit["page_id"] is None
    assert hit["source_kind"] == "library"
    # The other library-hit fields survive the strip.
    assert hit["path"] == "opencode/config.md"
    assert hit["title"] == "Config"
    assert hit["excerpt"] == "library excerpt"


def _drive_wiki_read(
    *,
    memory_service: Any,
    qmd_get_fn: Any = None,
) -> Any:
    """Drive the registered ``wiki_read`` closure end-to-end.

    Mirrors :func:`_drive_wiki_search`: register the librarian tools
    with the supplied fakes, locate the ``wiki_read`` closure in
    the agent's toolset, and return the bound function so the
    caller can invoke it with whatever ``page_ids`` it needs. The
    returned bodies dict is what the LLM agent sees when it calls
    ``wiki_read``.
    """
    from lies.agents import librarian as librarian_mod

    agent = librarian_mod.librarian_agent(model="test")
    librarian_mod.register_librarian_tools(
        agent,
        wiki=_FakeWiki(wiki_dir=Path("/tmp/fake-wiki")),  # type: ignore[arg-type]
        memory_service=memory_service,  # type: ignore[arg-type]
        qmd_get=qmd_get_fn,
    )

    tool_fn: object | None = None
    for toolset in agent.toolsets:
        tool = toolset.tools.get("wiki_read")
        if tool is not None:
            tool_fn = tool.function
            break
    assert tool_fn is not None, "wiki_read tool not registered"

    return tool_fn  # caller invokes with the page_ids list


def test_wiki_read_dispatches_wiki_ids() -> None:
    """``wiki_read(['page-abc123...'])`` calls ``memory_service.read``.

    Pre-fix behavior was correct for this branch; the test pins the
    dispatch so the source-aware change does not regress the
    wiki-only path. The fake ``memory_service.read`` records the
    IDs it was called with; the test asserts the wiki IDs were
    routed to it and not to ``qmd_get``.
    """
    seen: list[list[str]] = []

    class _WikiReadMemoryService:
        def read(self, ids: list[str]) -> dict[str, str]:
            seen.append(list(ids))
            return {pid: f"<body for {pid}>" for pid in ids}

    tool_fn = _drive_wiki_read(memory_service=_WikiReadMemoryService())
    bodies = tool_fn(None, ["page-abc123def456"])  # type: ignore[misc]

    assert seen == [["page-abc123def456"]]
    assert bodies == {"page-abc123def456": "<body for page-abc123def456>"}


def test_wiki_read_dispatches_library_paths_to_qmd() -> None:
    """``wiki_read(['opencode/config.md'])`` reads from the library's qmd chunks.

    Pre-fix bug (Task B fallback): ``wiki_read`` only knew wiki
    page IDs and raised ``WikiPageNotFound`` on library paths.
    The fix routes ``<collection>/<page>`` identifiers through
    ``qmd get`` against ``library_git_root()``.
    """
    from lies.library.registry import library_git_root

    qmd_calls: list[dict[str, Any]] = []

    class _EmptyMemoryService:
        def read(self, ids: list[str]) -> dict[str, str]:
            raise AssertionError(
                f"memory_service.read must NOT be called for library paths; got {ids!r}"
            )

    def fake_qmd_get(cwd: Path, qmd_path: str) -> str:
        qmd_calls.append({"cwd": cwd, "qmd_path": qmd_path})
        return f"<body for {qmd_path}>"

    tool_fn = _drive_wiki_read(
        memory_service=_EmptyMemoryService(),
        qmd_get_fn=fake_qmd_get,
    )
    bodies = tool_fn(None, ["opencode/config.md"])  # type: ignore[misc]

    assert qmd_calls == [{"cwd": library_git_root(), "qmd_path": "qmd://opencode/config.md"}], (
        "wiki_read must call qmd_get against the library git root"
    )
    assert bodies == {"opencode/config.md": "<body for qmd://opencode/config.md>"}


def test_wiki_read_unknown_raises() -> None:
    """``wiki_read(['unknown_format'])`` raises ``WikiPageNotFound``.

    IDs without a ``page-`` prefix and without a ``/`` separator
    are not wiki IDs and not library paths — the dispatch cannot
    route them, so the closure raises ``WikiPageNotFound`` (the
    same exception ``memory_service.read`` raises for unknown
    wiki IDs) so the LLM agent sees a single error path.
    """
    import pytest

    from lies.memory.models import WikiPageNotFound

    class _EmptyMemoryService:
        def read(self, ids: list[str]) -> dict[str, str]:  # pragma: no cover - unreachable
            raise AssertionError(f"memory_service.read must NOT be called; got {ids!r}")

    def fake_qmd_get(cwd: Path, qmd_path: str) -> str:  # pragma: no cover - unreachable
        raise AssertionError(f"qmd_get must NOT be called; got cwd={cwd} path={qmd_path}")

    tool_fn = _drive_wiki_read(
        memory_service=_EmptyMemoryService(),
        qmd_get_fn=fake_qmd_get,
    )
    with pytest.raises(WikiPageNotFound):
        tool_fn(None, ["unknown_format"])  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Fix-A - ``_wiki_read`` accepts ``qmd://``-prefixed library paths
# ---------------------------------------------------------------------------


def test_wiki_read_accepts_qmd_prefixed_library_paths() -> None:
    """``wiki_read(['qmd://opencode/config.md'])`` reads from the library's qmd chunks.

    Pre-fix bug (live debug session
    ``f39c9ef8-77a8-4f2d-86b7-a57a48bd82d5``): the LLM agent
    constructed ``qmd://opencode/config.md``-style identifiers from
    qmd search results and passed them to ``wiki_read``. The
    dispatch's ``"/" in pid`` library-path branch captured the full
    ``qmd://...`` string, and the ``_qmd_get_callable`` call then
    prepended ANOTHER ``qmd://`` -> ``qmd://qmd://opencode/config.md``
    -> ``QmdError`` -> ``WikiPageNotFound``. The fix strips a
    leading ``qmd://`` before the library-path branch and uses the
    bare path for the qmd_get URI; the body dict still keys by the
    raw input pid (preserves dedupe-by-input semantics).
    """
    from lies.library.registry import library_git_root

    qmd_calls: list[dict[str, Any]] = []

    class _EmptyMemoryService:
        def read(self, ids: list[str]) -> dict[str, str]:  # pragma: no cover - unreachable
            raise AssertionError(
                f"memory_service.read must NOT be called for library paths; got {ids!r}"
            )

    def fake_qmd_get(cwd: Path, qmd_path: str) -> str:
        qmd_calls.append({"cwd": cwd, "qmd_path": qmd_path})
        return f"<body for {qmd_path}>"

    tool_fn = _drive_wiki_read(
        memory_service=_EmptyMemoryService(),
        qmd_get_fn=fake_qmd_get,
    )
    bodies = tool_fn(None, ["qmd://opencode/config.md"])  # type: ignore[misc]

    # The fix must call qmd_get with a single ``qmd://`` prefix,
    # never ``qmd://qmd://...``. Library git root is the live one.
    assert qmd_calls == [{"cwd": library_git_root(), "qmd_path": "qmd://opencode/config.md"}], (
        "wiki_read must call qmd_get with the qmd:// URI, not double-prefixed"
    )
    # Body dict keys by the raw input pid (preserves dedupe-by-input
    # semantics — see brief's risk note).
    assert bodies == {"qmd://opencode/config.md": "<body for qmd://opencode/config.md>"}


def test_wiki_read_dispatches_mixed_wiki_and_qmd_prefixed_library_ids() -> None:
    """``wiki_read(['page-<sha1>', 'qmd://foo/bar.md'])`` dispatches each correctly.

    Pins the source-aware dispatch end-to-end: the wiki ID routes
    to ``memory_service.read`` and the ``qmd://``-prefixed library
    path routes to ``qmd_get`` against ``library_git_root()`` with
    a single ``qmd://`` URI prefix. Neither side leaks into the
    other's downstream call.
    """
    from lies.library.registry import library_git_root

    seen_wiki_ids: list[list[str]] = []
    qmd_calls: list[dict[str, Any]] = []

    class _MixedMemoryService:
        def read(self, ids: list[str]) -> dict[str, str]:
            seen_wiki_ids.append(list(ids))
            return {pid: f"<wiki body for {pid}>" for pid in ids}

    def fake_qmd_get(cwd: Path, qmd_path: str) -> str:
        qmd_calls.append({"cwd": cwd, "qmd_path": qmd_path})
        return f"<library body for {qmd_path}>"

    tool_fn = _drive_wiki_read(
        memory_service=_MixedMemoryService(),
        qmd_get_fn=fake_qmd_get,
    )
    bodies = tool_fn(  # type: ignore[misc]
        None,
        ["page-abc123def456", "qmd://foo/bar.md"],
    )

    # Wiki side: only the wiki ID was passed to memory_service.read,
    # not the qmd://-prefixed library path.
    assert seen_wiki_ids == [["page-abc123def456"]]
    # Library side: single qmd:// prefix, not double.
    assert qmd_calls == [{"cwd": library_git_root(), "qmd_path": "qmd://foo/bar.md"}]
    # Body dict keys by the raw input (wiki key bare, library key
    # carries its original qmd:// prefix).
    assert bodies == {
        "page-abc123def456": "<wiki body for page-abc123def456>",
        "qmd://foo/bar.md": "<library body for qmd://foo/bar.md>",
    }


# ---------------------------------------------------------------------------
# Fix-D2-extend - strip qmd ``docid`` from wiki and library hits
# ---------------------------------------------------------------------------


def test_wiki_search_strips_qmd_docid_from_wiki_hits() -> None:
    """Wiki hits must strip the qmd ``docid`` field along with ``page_id``.

    Pre-fix bug (live debug session
    ``d7554f5d-3d8d-4912-a57c-3dd266225a6d``): the wiki-side qmd call
    returns hits with ``docid='#d75430'`` (and ``file: 'qmd://...'``).
    Fix-D2 strips ``page_id`` but qmd hits don't have a ``page_id``
    field - they have ``docid``. The strip was a no-op, so the LLM
    agent extracted ``docid='#d75430'`` from the search result and
    passed it to ``wiki_read(['#d75430'])``. ``_wiki_read`` doesn't
    recognize the qmd docid format (no ``page-`` prefix, no ``/``
    separator) and raised ``WikiPageNotFound``. The fix strips
    ``docid`` alongside ``page_id`` so the LLM never sees a
    qmd-style identifier in the search results.
    """
    from lies.library.registry import library_git_root

    wiki_pages = [
        _FakeWikiEvidence(
            page_id="page-2da7bf8c551d",
            path="concepts/pydantic",
            collection_id="wiki",
            excerpt="Wiki excerpt",
        ),
    ]
    wiki_service = _FakeMemoryService(pages=wiki_pages, no_coverage=False)
    fake_wiki = _FakeWiki(wiki_dir=Path("/tmp/fake-wiki"))
    lib_root = library_git_root()
    wiki_qmd_hit = {
        "docid": "#d75430",
        "file": "qmd://opencode/config.md",
        "path": "concepts/pydantic",
        "title": "Wiki pydantic",
        "score": 0.7,
        "excerpt": "qmd excerpt",
    }

    def fake_qmd_query(cwd: Path, q: str, limit: int = 5, **_kw: Any) -> list[dict[str, Any]]:
        if Path(cwd) == fake_wiki.wiki_dir:
            return [wiki_qmd_hit]
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
    hit = hits[0]
    assert hit["source_kind"] == "wiki"
    assert "docid" not in hit
    assert hit["page_id"] == "page-2da7bf8c551d"
    assert hit["path"] == "concepts/pydantic"
    assert hit["file"] == "qmd://opencode/config.md"
    assert hit["title"] == "Wiki pydantic"
    assert hit["excerpt"] == "qmd excerpt"


def test_wiki_search_strips_qmd_docid_from_library_hits() -> None:
    """Library hits must strip the qmd ``docid`` field along with ``page_id``.

    Same Fix-D2-extension bug class as the wiki-side test: library
    qmd hits also surface ``docid='#d75430'`` (instead of
    ``page_id``) in some qmd versions; Fix-D2 only stripped
    ``page_id``. The strip must drop ``docid`` too so the LLM
    agent's downstream ``wiki_read`` call never receives a qmd
    docid.
    """
    from lies.library.registry import library_git_root

    wiki_service = _FakeMemoryService(pages=[], no_coverage=False)

    def fake_qmd_query(cwd: Path, q: str, limit: int = 5, **_kw: Any) -> list[dict[str, Any]]:
        if Path(cwd) == library_git_root():
            return [
                {
                    "docid": "#abc123",
                    "file": "qmd://opencode/config.md",
                    "path": "opencode/config.md",
                    "title": "Config",
                    "excerpt": "library excerpt",
                }
            ]
        return []

    out = _drive_wiki_search(memory_service=wiki_service, qmd_query_fn=fake_qmd_query)

    hits = out["hits"]
    assert len(hits) == 1
    hit = hits[0]
    assert hit["source_kind"] == "library"
    assert "docid" not in hit
    assert hit["page_id"] is None
    assert hit["path"] == "opencode/config.md"
    assert hit["title"] == "Config"
    assert hit["excerpt"] == "library excerpt"


def test_wiki_search_strips_qmd_docid_from_unmatched_wiki_hit() -> None:
    """Unmatched wiki qmd hits (path not in memory search) must also drop docid.

    Best-effort fallback path: a wiki qmd hit whose path doesn't
    match any wiki search hit gets ``page_id=None``. The same
    strip logic must drop ``docid`` so the LLM never sees a
    qmd-style identifier even on the unmatched (orphan) wiki
    path.
    """
    from lies.library.registry import library_git_root

    wiki_service = _FakeMemoryService(pages=[], no_coverage=False)
    fake_wiki = _FakeWiki(wiki_dir=Path("/tmp/fake-wiki"))
    lib_root = library_git_root()
    wiki_qmd_hit = {
        "docid": "#d75430",
        "file": "qmd://opencode/orphan.md",
        "path": "opencode/orphan",
        "title": "Orphan",
        "score": 0.7,
        "excerpt": "orphan excerpt",
    }

    def fake_qmd_query(cwd: Path, q: str, limit: int = 5, **_kw: Any) -> list[dict[str, Any]]:
        if Path(cwd) == fake_wiki.wiki_dir:
            return [wiki_qmd_hit]
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
    hit = hits[0]
    assert hit["source_kind"] == "wiki"
    assert "docid" not in hit
    assert hit["page_id"] is None
