"""The search tool against the live qmd daemon, not against a stub."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.integration


def _live_collections() -> frozenset[str]:
    """The daemon's own served set, read through the real seam.

    Not a ``search.py`` helper. A stub target that lives in ``src/``
    outlives the refactor that moved the call off it, and the stub
    then no-ops while the real code path runs — a test that looks
    stubbed and is not. ``access.qmd_collection_names`` is what
    ``validate_scope`` awaits, so stubbing it stubs the check.
    """
    from lies.qmd import access

    return asyncio.run(access.qmd_collection_names())


def _registry_from_daemon(monkeypatch: pytest.MonkeyPatch) -> frozenset[str]:
    """Point the LIES collection resolver at the daemon's served set.

    The autouse XDG isolation (``tests/conftest.py``) routes the
    registry at an empty ``tmp_path``, so ``_resolve_tag_collections``
    resolves every tag to nothing. The tests below are about the
    routing layer, not the registry, so the resolver is fed the
    daemon's own view.

    **Both** registry accessors, because ``_resolve_tag_collections``
    reads two of them and they are not the same one. Validation calls
    ``_collect_available_tags_mcp`` (built from
    ``library_collection_names``); the retriever then calls
    ``_collections_matching``, which imports
    ``library_collection_metas`` (``query/synthesizer.py:857``). Patching
    only the names accessor satisfies validation and then returns an
    empty match set, which ``search`` reports as the *whole* expression
    being an unknown tag::

        assert result["unknown_tags"] == []
        E  AssertionError: assert ['c:claude_code|c:opencode'] == []

    which reads as a resolver bug and is not one. The metas carry a
    ``name`` and ``tags``; with no real ``config.yaml`` under the
    isolated root there is nothing for it to read, so the daemon's set
    is synthesised directly. ``tags`` is left empty, which the implicit
    self-tag rule (``name in tags ∪ {name}``) covers — a ``c:`` atom is
    a strict name match and does not consult tags at all.

    Through ``monkeypatch`` rather than a bare assignment: a raw
    rebind of the registry module attribute outlives the test and
    silently re-points every later test's resolver at the live
    daemon's collection list.
    """
    import lies.library.registry as registry
    from lies.library.registry import LibraryCollectionMeta

    live = _live_collections()
    assert live, "the daemon reported no collections; every assertion below would be vacuous"
    monkeypatch.setattr(registry, "library_collection_names", lambda: live)
    monkeypatch.setattr(
        registry,
        "library_collection_metas",
        lambda: iter([LibraryCollectionMeta(name=n, tags=()) for n in sorted(live)]),
    )
    return live


def test_scoped_search_returns_rows_from_every_named_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-collection query returns rows from every named collection."""
    from lies.mcp.search import search

    _registry_from_daemon(monkeypatch)

    result = search.fn(
        question="PreToolUse hook matcher settings",
        tag_expr="c:claude_code|c:opencode",
    )

    assert result["unknown_tags"] == []
    assert set(result["searched_scope"]) == {"claude_code", "opencode"}, result["searched_scope"]
    assert result["hits"], "live corpus should match at least one row across claude_code+opencode"
    first_segments = {hit["path"].split("/", 1)[0] for hit in result["hits"]}
    assert first_segments <= {"claude_code", "opencode"}, (
        f"a hit's path first-segment fell outside the named scope: {first_segments!r}"
    )
    assert not result["no_coverage"]


def test_hits_carry_a_path_never_a_docid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hit's path is a ``<collection>/<page>`` form, never a docid."""
    from lies.mcp.search import search

    _registry_from_daemon(monkeypatch)

    result = search.fn(question="PreToolUse hook matcher settings", tag_expr="c:claude_code")

    # Without this the loop below iterates zero rows and asserts
    # nothing, so the test passes on the exact failure it exists to
    # catch.
    assert result["hits"], "live corpus should match at least one row in claude_code"
    for hit in result["hits"]:
        assert "file" in hit, "the daemon's `file` key should reach the envelope"
        assert "path" in hit, "the envelope should expose `path` for downstream consumers"
        assert not hit["path"].startswith("#"), (
            f"hit path is a docid, not a file path: {hit['path']!r}"
        )
        assert "/" in hit["path"], f"hit path is not collection/page form: {hit['path']!r}"


def test_unknown_collection_is_reported_not_silently_widened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown collection name short-circuits with ``unknown_tags``."""
    from lies.mcp.search import search

    _registry_from_daemon(monkeypatch)

    result = search.fn(
        question="anything",
        tag_expr="c:no_such_collection_for_test_xyz",
    )

    assert result["unknown_tags"] == ["c:no_such_collection_for_test_xyz"]
    assert result["searched_scope"] == []
    assert result["no_coverage"] is False
    assert result["hits"] == []
