from datetime import UTC, datetime

import pytest

from lies.library.config_io import (
    CollectionAlreadyExists,
    config_path_for,
    load_config,
    save_config,
)
from lies.library.record import LibraryCollectionConfig
from lies.library.paths import Library


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


def _sample_config(name: str = "claude_code") -> LibraryCollectionConfig:
    now = datetime(2026, 9, 13, 22, 17, 29, tzinfo=UTC)
    return LibraryCollectionConfig(
        name=name,
        source="https://example.com/llms.txt",
        tags=("skills",),
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=now,
        updated_at=now,
        config={},
    )


def test_config_path_for(library: Library) -> None:
    assert (
        config_path_for("claude_code") == library.collections_root / "claude_code" / "config.yaml"
    )


def test_save_then_load_round_trip(library: Library) -> None:
    cfg = _sample_config()
    save_config(cfg)
    loaded = load_config("claude_code")
    assert loaded.name == cfg.name
    assert loaded.source == cfg.source
    assert loaded.tags == cfg.tags


def test_load_missing_raises(library: Library) -> None:
    from lies.library.errors import CollectionNotFound

    with pytest.raises(CollectionNotFound):
        load_config("missing")


def test_save_existing_without_force_raises(library: Library) -> None:
    save_config(_sample_config())
    with pytest.raises(CollectionAlreadyExists):
        save_config(_sample_config())


@pytest.mark.slow
def test_save_existing_with_force_overwrites(library: Library) -> None:
    save_config(_sample_config())
    cfg2 = _sample_config()
    cfg2 = _sample_config().__class__(
        name=cfg2.name,
        source="https://other.example.com",
        tags=cfg2.tags,
        scraper_cmd=cfg2.scraper_cmd,
        doc_path=cfg2.doc_path,
        mapper_model=cfg2.mapper_model,
        language=cfg2.language,
        version=cfg2.version,
        created_at=cfg2.created_at,
        updated_at=cfg2.updated_at,
        config=cfg2.config,
    )
    save_config(cfg2, force=True)
    assert load_config("claude_code").source == "https://other.example.com"
