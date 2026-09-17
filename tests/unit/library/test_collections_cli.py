from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.library.collections_cli import library_collections_app
from lies.library.config_io import save_config
from lies.library.paths import Library
from lies.library.record import LibraryCollectionConfig


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


def test_list_emits_names(library: Library) -> None:
    save_config(_cfg("alpha"))
    save_config(_cfg("beta"))
    runner = CliRunner()
    app.add_typer(library_collections_app, name="library")
    result = runner.invoke(app, ["library", "list"])
    assert result.exit_code == 0
    assert "alpha" in result.stdout
    assert "beta" in result.stdout
