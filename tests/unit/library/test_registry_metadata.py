"""Library collection metadata surfaces tags + scope_keywords for collections_read."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lies.library.config_io import save_config
from lies.library.paths import Library
from lies.library.record import LibraryCollectionConfig
from lies.library.registry import LibraryCollectionMeta, library_collection_metas


def test_collection_meta_carries_tags_and_scope_keywords() -> None:
    """LibraryCollectionMeta carries tags + scope_keywords on the dataclass."""
    meta = LibraryCollectionMeta(
        name="alpha",
        source_url="https://example.test",
        tags=frozenset({"plugins", "cli"}),
        scope_keywords=frozenset({"plugin", "cli"}),
    )
    assert meta.tags == frozenset({"plugins", "cli"})
    assert meta.scope_keywords == frozenset({"plugin", "cli"})
    assert meta.source_url == "https://example.test"


def test_collection_meta_defaults_tags_and_scope_keywords_to_empty() -> None:
    """Default empty frozenset when caller omits the fields (back-compat)."""
    meta = LibraryCollectionMeta(name="alpha", source_url="x")
    assert meta.tags == frozenset()
    assert meta.scope_keywords == frozenset()


def test_collection_meta_defaults_source_url_to_empty() -> None:
    """``source_url`` defaults to empty string when caller omits it."""
    meta = LibraryCollectionMeta(name="alpha")
    assert meta.source_url == ""


def test_collection_meta_frozen() -> None:
    """``LibraryCollectionMeta`` stays frozen — no field reassignment."""
    meta = LibraryCollectionMeta(name="alpha", source_url="x")
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        meta.name = "beta"  # type: ignore[misc]


@pytest.fixture
def library(tmp_path, monkeypatch: pytest.MonkeyPatch):
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


def _cfg(
    name: str, *, tags: tuple[str, ...] = (), scope_keywords: tuple[str, ...] = ()
) -> LibraryCollectionConfig:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return LibraryCollectionConfig(
        name=name,
        source=f"https://example.com/{name}",
        tags=tags,
        scope_keywords=scope_keywords,
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=now,
        updated_at=now,
        config={},
    )


def test_library_collection_metas_propagates_tags_and_scope_keywords(
    library: Library,
) -> None:
    """``library_collection_metas`` carries tags + scope_keywords + source_url
    from the on-disk ``config.yaml`` to ``LibraryCollectionMeta``."""
    save_config(_cfg("alpha", tags=("plugins", "cli"), scope_keywords=("plugin", "cli")))
    metas = list(library_collection_metas())
    assert len(metas) == 1
    assert metas[0].name == "alpha"
    assert metas[0].tags == frozenset({"plugins", "cli"})
    assert metas[0].scope_keywords == frozenset({"plugin", "cli"})
    assert metas[0].source_url == "https://example.com/alpha"


def test_library_collection_metas_defaults_when_tags_omitted(library: Library) -> None:
    """Back-compat: old configs without tags/scope_keywords surface as empty frozensets."""
    save_config(_cfg("legacy"))
    metas = list(library_collection_metas())
    assert len(metas) == 1
    assert metas[0].tags == frozenset()
    assert metas[0].scope_keywords == frozenset()
    assert metas[0].source_url == "https://example.com/legacy"
