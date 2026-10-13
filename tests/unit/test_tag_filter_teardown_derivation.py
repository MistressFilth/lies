"""The tag-filter fixture's teardown set to the union it must remove."""

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
    """The wiki-named collection is part of the fixture-owned set."""
    wiki = _wiki_named("some-lib", tmp_path)
    registered = set(_registered_by_this_fixture(wiki))

    assert set(FIXTURE_COLLECTIONS).issubset(registered), (
        f"_registered_by_this_fixture must include every FIXTURE_COLLECTIONS "
        f"member; missing={set(FIXTURE_COLLECTIONS) - registered!r}, "
        f"got={registered!r}"
    )
    assert f"wiki_{wiki.name}" in registered, (
        f"_registered_by_this_fixture must include the wiki-named collection "
        f"wiki_{wiki.name}; got={registered!r}"
    )


def test_registered_by_this_fixture_tracks_the_wiki_name(
    tmp_path: Path,
) -> None:
    """A renamed wiki produces a different wiki collection in the set."""
    wiki_a = _wiki_named("alpha-lib", tmp_path)
    wiki_b = _wiki_named("beta-lib", tmp_path)
    registered_a = set(_registered_by_this_fixture(wiki_a))
    registered_b = set(_registered_by_this_fixture(wiki_b))

    assert "wiki_alpha-lib" in registered_a
    assert "wiki_beta-lib" in registered_b
    assert "wiki_alpha-lib" not in registered_b
    assert "wiki_beta-lib" not in registered_a


def test_unseed_qmd_iterates_the_fixture_owned_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The teardown removes every collection :func:`_registered_by_this_fixture` returns."""
    wiki = _wiki_named("tag-filter-lib", tmp_path)
    expected = set(_registered_by_this_fixture(wiki))

    removed: list[str] = []
    monkeypatch.setattr(
        "tests.integration.test_tag_filter_end_to_end.qmd_collection_remove",
        lambda cwd, name: removed.append(name),
    )
    # The post-condition check would otherwise invoke
    # ``qmd collection list`` and fail before the assertion. An
    # empty set says "the collection is no longer registered" —
    # the truth this test cares about — and avoids the subprocess.
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
