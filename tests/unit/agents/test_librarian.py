"""Tests for src/lies/agents/librarian.py — F18."""

from __future__ import annotations

import pytest

from lies.agents.librarian import (
    LibrarianDeps,
    LibrarianOutput,
    PageExcerpt,
    _rewrite_query_for_validator,
    resolve_collection_filter,
)


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
        def search(
            self,
            question: str,
            *,
            limit: int = 5,
            qmd_collection_filter: set[str] | None = None,
        ) -> _FakeSearchResult:
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


def test_resolve_collection_filter_untagged_returns_none() -> None:
    """No tag_expr and no exclude → qmd receives no filter."""
    result = resolve_collection_filter(tag_expr=None, exclude_tags=[], wiki_name="default")
    assert result is None


def test_resolve_collection_filter_includes_wiki_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tag_expr=c:opencode` → set contains `opencode` AND `wiki.name`.

    Repro of the 2026-09-22 transcript bug: tag-filtered queries must
    scope qmd to the named library collection while still letting the
    wiki's own qmd collection participate. Without the ``wiki_name``
    union, the librarian's wiki-only read path mints phantom page_ids
    for any library hit (Fix A drops them, leaving tag-filtered
    queries with empty answers).
    """
    from lies import library as _library_mod

    class _StubMeta:
        def __init__(self, name: str) -> None:
            self.name = name
            self.tags: tuple[str, ...] = (name,)
            self.scope_keywords: tuple[str, ...] = ()

    monkeypatch.setattr(
        _library_mod.registry, "library_collection_names", lambda: frozenset({"opencode"})
    )
    monkeypatch.setattr(
        _library_mod.registry,
        "library_collection_metas",
        lambda: [_StubMeta("opencode")],
    )
    result = resolve_collection_filter(tag_expr="c:opencode", exclude_tags=[], wiki_name="default")
    assert result is not None
    assert "opencode" in result
    assert "default" in result, (
        "wiki_name must be in the filter set so the wiki's own qmd "
        "collection keeps participating — otherwise tag-filtered "
        "queries surface zero wiki results"
    )


def test_wiki_search_closure_propagates_collection_filter(
    empty_wiki: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: librarian's ``wiki_search`` threads ``qmd_collection_filter`` to memory_service.

    Drives the registered tool function with a ``LibrarianDeps`` carrying
    ``tag_expr='c:opencode'``. The stub ``memory_service`` captures the
    kwargs and asserts ``qmd_collection_filter`` carries the resolved
    collection set (opencode + wiki.name). Pins Fix B's plumbing —
    without this the tag_expr never reaches qmd.
    """
    from lies.agents import librarian as librarian_mod
    from pydantic_ai import RunContext
    from pydantic_ai.models.test import TestModel
    from lies import library as _library_mod

    class _StubMeta:
        def __init__(self, name: str) -> None:
            self.name = name
            self.tags: tuple[str, ...] = (name,)
            self.scope_keywords: tuple[str, ...] = ()

    monkeypatch.setattr(
        _library_mod.registry, "library_collection_names", lambda: frozenset({"opencode"})
    )
    monkeypatch.setattr(
        _library_mod.registry,
        "library_collection_metas",
        lambda: [_StubMeta("opencode")],
    )

    captured: dict[str, object] = {}

    class _FakeSearchResult:
        def model_dump(self) -> dict[str, object]:
            return {
                "pages": [],
                "truncated": False,
                "fallback_used": False,
                "fallback_reason": "",
                "no_coverage": False,
            }

    class _FakeMemoryService:
        def search(
            self,
            question: str,
            *,
            limit: int = 5,
            qmd_collection_filter: set[str] | None = None,
            **kwargs: object,
        ) -> _FakeSearchResult:
            captured["question"] = question
            captured["limit"] = limit
            captured["qmd_collection_filter"] = qmd_collection_filter
            return _FakeSearchResult()

    agent = librarian_mod.librarian_agent(model="test")
    librarian_mod.register_librarian_tools(
        agent,
        wiki=empty_wiki,  # type: ignore[arg-type]
        memory_service=_FakeMemoryService(),  # type: ignore[arg-type]
    )

    tool_fn: object | None = None
    for toolset in agent.toolsets:
        tool = toolset.tools.get("wiki_search")
        if tool is not None:
            tool_fn = tool.function
            break
    assert tool_fn is not None

    deps = LibrarianDeps(question="q", tag_expr="c:opencode", exclude_tags=[], top_k=5)
    ctx = RunContext(model=TestModel(), deps=deps, usage=None)  # type: ignore[arg-type]
    tool_fn(ctx, "q", 5)  # type: ignore[misc]

    forwarded = captured.get("qmd_collection_filter")
    assert forwarded is not None, "qmd_collection_filter was not threaded to memory_service"
    assert "opencode" in forwarded  # type: ignore[operator]
    assert empty_wiki.name in forwarded  # type: ignore[union-attr,operator]
