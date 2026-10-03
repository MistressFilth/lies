"""Pin the tag-filter fixture's teardown set to the union it must remove.

The fixture at ``tests/integration/test_tag_filter_end_to_end.py``
registers four collections in qmd's throwaway per-test index, and the
product's :func:`lies.wiki.layout.ensure_wiki_qmd_registered` registers
``wiki_<wiki.name>`` whenever the test's orchestrator is asked a question
(via :func:`Orchestrator.run_query`). The teardown is responsible for
removing every collection the fixture (or any reachable product path)
added, not just the four it seeded -- a hardcoded
``FIXTURE_COLLECTIONS`` iteration left the ``wiki_<name>`` collection
behind in the live index on 2026-10-03 (five documents, collection
absent from ``store_collections``).

The single source of truth is
:func:`tests.integration.test_tag_filter_end_to_end._registered_by_this_fixture`,
and the three tests below pin its three load-bearing properties:

1. The set is a superset of the four fixture collections plus the
   wiki-named collection.
2. The wiki collection tracks ``wiki.name`` rather than a literal --
   a renamed wiki produces a different collection in the set.
3. The teardown iterates exactly the set :func:`_registered_by_this_fixture`
   returns. A regression that reverts to iterating
   ``FIXTURE_COLLECTIONS`` alone leaves the wiki collection out of the
   recorded removals and this test fails with a precise diff.

The tests run without ``INTEGRATION=1``: they stub
``qmd_collection_remove`` at the integration test module's import
point (the name the production code sees) and assert against the
recorded removals, so no real subprocess is spawned and the live
index is never touched. They live under ``tests/unit/`` rather than
``tests/integration/`` because the integration directory's conftest
applies a session-wide skip on every collected test unless opted in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.wiki.wiki import Wiki
from tests.integration.test_tag_filter_end_to_end import (
    FIXTURE_COLLECTIONS,
    _registered_by_this_fixture,
    _unseed_qmd,
)


def _wiki_named(wiki_name: str, tmp_path: Path) -> Wiki:
    """Build a Wiki with arbitrary name; the test asserts against the name."""
    return Wiki(
        name=wiki_name,
        data_root=tmp_path / "data",
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )


def test_registered_by_this_fixture_includes_the_wiki_collection(
    tmp_path: Path,
) -> None:
    """The wiki-named collection is part of the fixture-owned set.

    Pins that the union is correct and that the wiki collection
    tracks ``wiki.name`` rather than a literal. Without this, a
    future change that re-derives the set from a different source --
    only :data:`FIXTURE_COLLECTIONS`, or a hardcoded list of wiki
    names -- loses the union this branch depends on.
    """
    wiki = _wiki_named("some-lib", tmp_path)
    registered = set(_registered_by_this_fixture(wiki))

    # The four fixture collections are always present.
    assert set(FIXTURE_COLLECTIONS).issubset(registered), (
        f"_registered_by_this_fixture must include every FIXTURE_COLLECTIONS "
        f"member; missing={set(FIXTURE_COLLECTIONS) - registered!r}, "
        f"got={registered!r}"
    )
    # The wiki-named collection is always present and tracks the name.
    assert f"wiki_{wiki.name}" in registered, (
        f"_registered_by_this_fixture must include the wiki-named collection "
        f"wiki_{wiki.name}; got={registered!r}"
    )


def test_registered_by_this_fixture_tracks_the_wiki_name(
    tmp_path: Path,
) -> None:
    """A renamed wiki produces a different wiki collection in the set.

    Pins that the wiki collection is *derived* from ``wiki.name`` and
    not a literal. A regression that hardcoded ``wiki_tag-filter-lib``
    would silently keep the wrong collection in the set under a
    different wiki name -- a leak that a wiki-name-driven iteration
    set would catch by construction.
    """
    wiki_a = _wiki_named("alpha-lib", tmp_path)
    wiki_b = _wiki_named("beta-lib", tmp_path)
    registered_a = set(_registered_by_this_fixture(wiki_a))
    registered_b = set(_registered_by_this_fixture(wiki_b))

    assert "wiki_alpha-lib" in registered_a
    assert "wiki_beta-lib" in registered_b
    # And not the other way round.
    assert "wiki_alpha-lib" not in registered_b
    assert "wiki_beta-lib" not in registered_a


def test_unseed_qmd_iterates_the_fixture_owned_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The teardown removes every collection :func:`_registered_by_this_fixture` returns.

    The load-bearing contract: a future code path that registers a
    collection the fixture is responsible for cleaning up must fail
    this test if the teardown does not iterate the same set. The test
    stubs ``qmd_collection_remove`` at the integration test module's
    import (the name the production code sees) and stubs
    ``_registered_collections`` (the post-condition check the
    teardown runs after every removal) so the iteration is recorded
    without spawning a real subprocess or touching the live index.
    A regression that reverts to ``for coll in FIXTURE_COLLECTIONS``
    leaves the wiki-named collection out of the recorded removals
    and the assertion fails with a precise diff that names both
    ``missing from removed`` and ``unexpectedly removed``.
    """
    wiki = _wiki_named("tag-filter-lib", tmp_path)
    expected = set(_registered_by_this_fixture(wiki))

    removed: list[str] = []
    monkeypatch.setattr(
        "tests.integration.test_tag_filter_end_to_end.qmd_collection_remove",
        lambda cwd, name: removed.append(name),
    )
    # The post-condition check (``coll in _registered_collections(wiki.data_root)``)
    # is real on a fixture run with a real daemon; here it would invoke
    # ``qmd collection list`` and fail before the assertion. An empty
    # set says "the collection is no longer registered" -- the truth
    # this test cares about -- and avoids the real subprocess.
    monkeypatch.setattr(
        "tests.integration.test_tag_filter_end_to_end._registered_collections",
        lambda cwd: set(),
    )

    _unseed_qmd(wiki)

    assert set(removed) == expected, (
        f"_unseed_qmd must iterate _registered_by_this_fixture(wiki) exactly; "
        f"removed={sorted(removed)!r}, expected={sorted(expected)!r}, "
        f"missing from removed={expected - set(removed)!r}, "
        f"unexpectedly removed={set(removed) - expected!r}"
    )
