from datetime import UTC, datetime

import pytest

from lies.library.config_io import save_config
from lies.library.paths import Library
from lies.library.record import LibraryCollectionConfig
from lies.library.registry import (
    library_collection_record,
    library_collection_records,
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


def test_records_iterates_each_config(library: Library) -> None:
    save_config(_cfg("alpha"))
    save_config(_cfg("beta"))
    names = sorted(r.name for r in library_collection_records())
    assert names == ["alpha", "beta"]


def test_record_lookup_returns_none_for_missing(library: Library) -> None:
    assert library_collection_record("nope") is None


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
