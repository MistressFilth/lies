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

    Through ``monkeypatch`` rather than a bare assignment: a raw
    rebind of the registry module attribute outlives the test and
    silently re-points every later test's resolver at the live
    daemon's collection list.
    """
    import lies.library.registry as registry

    live = _live_collections()
    assert live, "the daemon reported no collections; every assertion below would be vacuous"
    monkeypatch.setattr(registry, "library_collection_names", lambda: live)
    return live


def test_scoped_search_returns_rows_from_every_named_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-collection query returns rows from every named collection.

    A global rank with rows dropped afterwards can return rows from
    only one named collection when all three have matches.
    """
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
    """Every hit's path is a ``<collection>/<page>`` form, never a docid.

    A hit keyed by docid silently returns the wrong file on
    collision. The daemon's ``query`` tool always sends ``file``
    as ``displayPath``; this pins that the contract holds against
    the live corpus.
    """
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
    """An unknown collection name short-circuits with ``unknown_tags``.

    The daemon answers an unknown collection with an empty result
    and no error. The pre-check in ``_search_impl`` asks the
    daemon's ``status`` tool for the real collection list and
    surfaces an unknown name in ``unknown_tags`` instead of
    returning empty ``hits`` (which a caller would read as
    "the corpus has nothing" — a false claim about the corpus).

    The assertion below is only meaningful if the name is rejected
    by the *daemon* pre-check and not by the local resolver: with an
    empty ``live_collections`` set, every name resolves to unknown
    locally and the pre-check never runs. ``_registry_from_daemon``
    asserts the set is non-empty, which is what separates the two.
    """
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
