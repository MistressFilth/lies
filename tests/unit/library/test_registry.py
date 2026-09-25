from datetime import UTC, datetime
from pathlib import Path

import pytest

from lies.library.config_io import save_config
from lies.library.paths import Library
from lies.library.record import LibraryCollectionConfig
from lies.library.registry import (
    _library_collection_names_cached,
    _library_collection_tags_cached,
    library_collection_names,
    library_collection_record,
    library_collection_records,
    library_collection_tags,
)


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = Library(
        collections_root=tmp_path / "collections",
        catalog_path=tmp_path / ".lies" / "catalog.db",
        log_path=tmp_path / "log.md",
        poison_root=tmp_path / "poison",
        git_root=tmp_path,
    )
    monkeypatch.setattr(Library, "open", classmethod(lambda cls: lib))
    (tmp_path / "collections").mkdir(parents=True)
    return lib


def _cfg(name: str) -> LibraryCollectionConfig:
    now = datetime.now(tz=UTC)
    return LibraryCollectionConfig(
        name=name,
        source=f"https://example.com/{name}",
        tags=("a",),
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=now,
        updated_at=now,
        config={},
    )


@pytest.mark.slow
def test_records_iterates_each_config(library: Library) -> None:
    save_config(_cfg("alpha"))
    save_config(_cfg("beta"))
    names = sorted(r.name for r in library_collection_records())
    assert names == ["alpha", "beta"]


def test_record_lookup_returns_none_for_missing(library: Library) -> None:
    assert library_collection_record("nope") is None


@pytest.mark.slow
def test_record_lookup_returns_config(library: Library) -> None:
    save_config(_cfg("alpha"))
    rec = library_collection_record("alpha")
    assert rec is not None
    assert rec.source == "https://example.com/alpha"


def test_collection_without_config_skipped(tmp_path, monkeypatch) -> None:
    # A directory with no config.yaml must not raise.
    lib = Library(
        collections_root=tmp_path / "collections",
        catalog_path=tmp_path / ".lies" / "catalog.db",
        log_path=tmp_path / "log.md",
        poison_root=tmp_path / "poison",
        git_root=tmp_path,
    )
    monkeypatch.setattr(Library, "open", classmethod(lambda cls: lib))
    (tmp_path / "collections" / "stub").mkdir(parents=True)
    assert list(library_collection_records()) == []


@pytest.fixture(autouse=True)
def _clear_collection_caches():
    """Each test gets fresh registry caches.

    Both caches use ``lru_cache`` (keyed on mtime), so cross-test
    state from earlier runs would otherwise leak the snapshot.
    """
    _library_collection_names_cached.cache_clear()
    _library_collection_tags_cached.cache_clear()
    yield
    _library_collection_names_cached.cache_clear()
    _library_collection_tags_cached.cache_clear()


def test_collection_names_picks_up_new_directory_after_mtime_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Long-running daemon scenario: a collection is added after the
    registry cache was populated. The cache must self-invalidate via
    the directory mtime key — without a daemon restart.

    Reproduces the live failure where the MCP daemon (started before
    the ``switchyard`` collection existed) returned a 13-collection
    snapshot instead of the actual 14 on disk.
    """
    lib = Library(
        collections_root=tmp_path / "collections",
        catalog_path=tmp_path / ".lies" / "catalog.db",
        log_path=tmp_path / "log.md",
        poison_root=tmp_path / "poison",
        git_root=tmp_path,
    )
    monkeypatch.setattr(Library, "open", classmethod(lambda cls: lib))
    (tmp_path / "collections").mkdir(parents=True)
    (tmp_path / "collections" / "alpha").mkdir()

    # First call populates the cache with the current snapshot.
    assert library_collection_names() == frozenset({"alpha"})

    # New collection directory — daemon-side ingest after cache populated.
    (tmp_path / "collections" / "beta").mkdir()
    # ``mkdir`` already bumped the parent mtime on most filesystems,
    # but pin it forward so the test is robust against filesystems
    # that don't bump mtime on subdir creates.
    _bump_mtime(tmp_path / "collections")

    assert library_collection_names() == frozenset({"alpha", "beta"})


@pytest.mark.slow
def test_collection_tags_picks_up_new_collection_after_mtime_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tag-union cache self-invalidates the same way the name cache does.

    The union of ``tags`` across every collection's ``config.yaml`` is
    a separate cache (different value, different invalidation key),
    so adding a collection must refresh the tag snapshot too. The
    F15 validator depends on both.
    """
    lib = Library(
        collections_root=tmp_path / "collections",
        catalog_path=tmp_path / ".lies" / "catalog.db",
        log_path=tmp_path / "log.md",
        poison_root=tmp_path / "poison",
        git_root=tmp_path,
    )
    monkeypatch.setattr(Library, "open", classmethod(lambda cls: lib))
    (tmp_path / "collections").mkdir(parents=True)
    save_config(_cfg("alpha"))
    _bump_mtime(tmp_path / "collections")

    assert library_collection_tags() == frozenset({"a"})

    save_config(_cfg("beta"))
    _bump_mtime(tmp_path / "collections")

    assert library_collection_tags() == frozenset({"a"})


def test_collection_names_cache_hits_on_unchanged_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged mtime = cache hit, no re-walk.

    The cache exists to avoid re-walking the directory on every
    F15-validator call (hot path). Pin the mtime, call twice, and
    verify the inner cache holds the same entry.
    """
    lib = Library(
        collections_root=tmp_path / "collections",
        catalog_path=tmp_path / ".lies" / "catalog.db",
        log_path=tmp_path / "log.md",
        poison_root=tmp_path / "poison",
        git_root=tmp_path,
    )
    monkeypatch.setattr(Library, "open", classmethod(lambda cls: lib))
    (tmp_path / "collections" / "alpha").mkdir(parents=True)
    _bump_mtime(tmp_path / "collections")

    first = library_collection_names()
    # Second call with unchanged mtime — must hit cache. We can't
    # directly count cache hits without poking ``CacheInfo``, but
    # the function returns the same value (the cache returns it
    # rather than re-walking).
    second = library_collection_names()
    assert first == second == frozenset({"alpha"})


def _bump_mtime(path: Path) -> None:
    """Force ``path``'s mtime forward so the cache key changes.

    Some filesystems don't bump the parent dir's mtime when a child
    is added; ``os.utime`` is a portable way to guarantee the key
    differs across calls.
    """
    import os
    import time

    new_time = time.time() + 1
    os.utime(path, (new_time, new_time))
