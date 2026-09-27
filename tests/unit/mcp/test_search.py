"""search() runs a single hybrid vec+lex qmd query across the resolved collection set."""

from __future__ import annotations

from typing import Any

import pytest


def _patch_registry(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    from lies.library.registry import LibraryCollectionMeta

    metas = [
        LibraryCollectionMeta(
            name=n,
            source_url=f"https://example.test/{n}",
            tags=frozenset({"plugins"}),
            scope_keywords=frozenset(),
        )
        for n in names
    ]
    monkeypatch.setattr("lies.library.registry.library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr("lies.library.registry.library_collection_names", lambda: frozenset(names))


def test_search_builds_hybrid_query_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    """search() calls qmd with a structured doc carrying vec+lex legs."""
    from lies.mcp.search import _build_query_doc

    doc = _build_query_doc("how do I author a plugin?", scope=["alpha", "beta"])
    # The structured doc must have BOTH legs (vec + lex).
    assert "vec:" in doc
    assert "lex:" in doc
    assert "how do I author a plugin?" in doc


def test_search_resolves_tag_expr_to_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    """tag_expr='c:alpha|c:beta' resolves to {alpha, beta} (sorted)."""
    from lies.mcp.search import _resolve_tag_collections

    _patch_registry(monkeypatch, ["alpha", "beta", "gamma"])

    out = _resolve_tag_collections("c:alpha|c:beta")
    assert out == (["alpha", "beta"], [])


def test_search_unknown_tag_marks_unknown_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown tag surfaces in unknown_tags; no daemon call made."""
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha"])

    qmd_called: list[Any] = []
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda *a, **kw: qmd_called.append((a, kw)) or [],
    )
    result = search.fn(
        question="anything",
        tag_expr="c:nope",
    )
    assert result["unknown_tags"] == ["c:nope"]
    assert result["no_coverage"] is False
    assert result["searched_scope"] == []
    assert result["hits"] == []
    assert qmd_called == [], "must short-circuit on unknown tag"


def test_search_returns_searched_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """search() returns searched_scope = sorted resolved collection names."""
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha", "beta", "gamma"])
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda doc, scope, limit, timeout: [
            {"path": f"{scope[0]}/page.md", "title": "P", "score": 0.5, "snippet": ""}
        ],
    )
    result = search.fn(question="hi", tag_expr="c:alpha|c:beta")
    assert result["searched_scope"] == ["alpha", "beta"]


def test_search_no_coverage_when_qmd_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """no_coverage=True when corpus has pages but query matched none."""
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha"])
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda doc, scope, limit, timeout: [],
    )
    result = search.fn(question="anything")
    # corpus has pages but qmd returned 0 hits
    assert result["no_coverage"] is True
    assert result["hits"] == []


def test_search_qmd_down_returns_no_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    """qmd unreachable → no_coverage=True, fallback_reason set."""
    from lies.mcp.search import search
    from lies.qmd.cli import QmdCommandError

    _patch_registry(monkeypatch, ["alpha"])

    def boom(*a: object, **kw: object) -> list[dict[str, Any]]:
        raise QmdCommandError("qmd down")

    monkeypatch.setattr("lies.mcp.search._post_query", boom)
    result = search.fn(question="anything")
    assert result["no_coverage"] is True
    assert "qmd" in (result.get("fallback_reason") or "").lower()


def test_search_hit_is_top_ranked(monkeypatch: pytest.MonkeyPatch) -> None:
    """search() result.hit = first element of hits (top-ranked row)."""
    from lies.mcp.search import search

    _patch_registry(monkeypatch, ["alpha"])
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda doc, scope, limit, timeout: [
            {"path": "alpha/a.md", "title": "A", "score": 0.9, "snippet": "s"},
            {"path": "alpha/b.md", "title": "B", "score": 0.5, "snippet": "s"},
        ],
    )
    result = search.fn(question="hi")
    assert result["hit"]["path"] == "alpha/a.md"
    assert result["hits"][0]["path"] == "alpha/a.md"
    assert result["hits"][1]["path"] == "alpha/b.md"
