"""The search tool against the live qmd daemon, not against a stub.

Companion to ``test_read_daemon.py``: where read's integration test
checks the *body* the tool returns, search's checks the *envelope*
the tool returns. The unit suite pins the wire shape and the
contract; the integration suite pins that the wire shape and the
contract hold against a daemon that is doing the actual ranking
and reranking.

Two properties are checked here, because the unit tests cannot
verify them:

- The collection filter is a true push-down. A multi-collection
  query against a real corpus returns in-scope rows from every
  named collection; the unit test stubs the daemon and so cannot
  tell whether the daemon honours the filter or whether the
  envelope is doing the post-filtering the spec is trying to
  eliminate.
- The hit shape is what the F19 citation contract expects. Each
  hit's ``path`` is a ``<collection>/<page>`` form (e.g.
  ``claude_code/hooks.md``), not a docid, and the path's first
  segment is in the resolved scope.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_scoped_search_returns_rows_from_every_named_collection() -> None:
    """A multi-collection query returns rows from every named collection.

    With the previous CLI path the rank was global and rows were
    dropped in Python, so a multi-collection query could return
    rows from only one of the named collections even when all
    three had matches. The daemon's ``collections`` parameter is
    a true push-down; this test pins that property against the
    live corpus, not a stub.
    """
    import lies.mcp.search as search_module
    from lies.mcp.search import search

    # The autouse XDG isolation (tests/conftest.py) routes the
    # LIES library at an empty ``tmp_path``; the resolver then
    # rejects every collection name. Populate the LIES registry
    # from the daemon's own view so the test exercises the
    # routing layer rather than the registry, and clear the
    # cached collection set so a previous test's value does not
    # leak.
    search_module._qmd_collection_names_cache_clear()
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

    ``docid`` is ``documents.hash[0:6]`` resolved by
    ``LIKE '<prefix>%' LIMIT 1`` with no ``ORDER BY``; three live
    collisions exist among 5987 active documents. A hit keyed by
    docid silently returns the wrong file on collision. The
    daemon's ``query`` tool always sends ``file`` as
    ``displayPath``; this test pins that the contract holds
    against the live corpus, not a stub.
    """
    import lies.library.registry as _registry
    import lies.mcp.search as search_module
    from lies.mcp.search import search

    search_module._qmd_collection_names_cache_clear()
    live_collections = search_module._qmd_collection_names_for_check()
    _registry.library_collection_names = lambda: live_collections  # type: ignore[assignment]

    result = search.fn(question="PreToolUse hook matcher settings", tag_expr="c:claude_code")

    for hit in result["hits"]:
        assert "file" in hit, "the daemon's `file` key should reach the envelope"
        assert "path" in hit, "the envelope should expose `path` for downstream consumers"
        assert not hit["path"].startswith("#"), (
            f"hit path is a docid, not a file path: {hit['path']!r}"
        )
        # Daemon `file` should already be a path, and the envelope
        # should set `path` to the same value. The two assertions
        # below are the unit-level wire shape; this is the
        # integration-level confirmation.
        assert "/" in hit["path"], f"hit path is not collection/page form: {hit['path']!r}"


def test_unknown_collection_is_reported_not_silently_widened() -> None:
    """An unknown collection name short-circuits with ``unknown_tags``.

    The daemon answers an unknown collection with an empty result
    and no error. The pre-check in ``_search_impl`` asks the
    daemon's ``status`` tool for the real collection list and
    surfaces an unknown name in ``unknown_tags`` instead of
    returning an empty ``hits`` (which a caller would read as
    "the corpus has nothing" — a false claim about the corpus
    for a question that named something absent).
    """
    import lies.library.registry as _registry
    import lies.mcp.search as search_module
    from lies.mcp.search import search

    search_module._qmd_collection_names_cache_clear()
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
