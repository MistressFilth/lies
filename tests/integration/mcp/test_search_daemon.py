"""The search tool against the live qmd daemon, not against a stub.

The unit suite pins the wire shape; this pins that it holds against a
daemon doing the real ranking and reranking. Two properties only a
live daemon can verify: the collection filter is a true push-down
rather than the envelope post-filtering, and each hit's ``path`` is a
``<collection>/<page>`` form and never a docid. ``docid`` is
``documents.hash[0:6]`` resolved by ``LIKE '<prefix>%' LIMIT 1`` with
no ``ORDER BY``; three live prefix collisions exist among 5987 active
documents.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_scoped_search_returns_rows_from_every_named_collection() -> None:
    """A multi-collection query returns rows from every named collection.

    A global rank with rows dropped afterwards can return rows from
    only one named collection when all three have matches.
    """
    import lies.mcp.search as search_module
    from lies.mcp.search import search

    # The autouse XDG isolation (tests/conftest.py) routes the
    # LIES library at an empty ``tmp_path``; the resolver then
    # rejects every collection name. Populate the LIES registry
    # from the daemon's own view so the test exercises the
    # routing layer rather than the registry.
    live_collections = search_module._qmd_collection_names_for_check()

    import lies.library.registry as _registry

    _registry.library_collection_names = lambda: live_collections  # type: ignore[assignment]

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


def test_hits_carry_a_path_never_a_docid() -> None:
    """Every hit's path is a ``<collection>/<page>`` form, never a docid.

    A hit keyed by docid silently returns the wrong file on
    collision. The daemon's ``query`` tool always sends ``file``
    as ``displayPath``; this pins that the contract holds against
    the live corpus.
    """
    import lies.library.registry as _registry
    import lies.mcp.search as search_module
    from lies.mcp.search import search

    live_collections = search_module._qmd_collection_names_for_check()
    _registry.library_collection_names = lambda: live_collections  # type: ignore[assignment]

    result = search.fn(question="PreToolUse hook matcher settings", tag_expr="c:claude_code")

    for hit in result["hits"]:
        assert "file" in hit, "the daemon's `file` key should reach the envelope"
        assert "path" in hit, "the envelope should expose `path` for downstream consumers"
        assert not hit["path"].startswith("#"), (
            f"hit path is a docid, not a file path: {hit['path']!r}"
        )
        assert "/" in hit["path"], f"hit path is not collection/page form: {hit['path']!r}"


def test_unknown_collection_is_reported_not_silently_widened() -> None:
    """An unknown collection name short-circuits with ``unknown_tags``.

    The daemon answers an unknown collection with an empty result
    and no error. The pre-check in ``_search_impl`` asks the
    daemon's ``status`` tool for the real collection list and
    surfaces an unknown name in ``unknown_tags`` instead of
    returning empty ``hits`` (which a caller would read as
    "the corpus has nothing" — a false claim about the corpus).
    """
    import lies.library.registry as _registry
    import lies.mcp.search as search_module
    from lies.mcp.search import search

    live_collections = search_module._qmd_collection_names_for_check()
    _registry.library_collection_names = lambda: live_collections  # type: ignore[assignment]

    result = search.fn(
        question="anything",
        tag_expr="c:no_such_collection_for_test_xyz",
    )

    assert result["unknown_tags"] == ["c:no_such_collection_for_test_xyz"]
    assert result["searched_scope"] == []
    assert result["no_coverage"] is False
    assert result["hits"] == []
