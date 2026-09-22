import pytest

from lies.library.bootstrap import bootstrap_library_collection
from lies.library.config_io import load_config
from lies.library.errors import CollectionMismatch
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


def test_bootstrap_creates_config_when_missing(library: Library) -> None:
    rec = bootstrap_library_collection("foo", "https://example.com/foo.txt")
    assert rec.name == "foo"
    assert rec.source == "https://example.com/foo.txt"
    assert load_config("foo").source == "https://example.com/foo.txt"


def test_bootstrap_returns_existing_when_source_matches(library: Library) -> None:
    bootstrap_library_collection("foo", "https://example.com/foo.txt")
    rec = bootstrap_library_collection("foo", "https://example.com/foo.txt")
    assert rec.source == "https://example.com/foo.txt"


def test_bootstrap_raises_mismatch_when_source_differs(library: Library) -> None:
    bootstrap_library_collection("foo", "https://example.com/foo.txt")
    with pytest.raises(CollectionMismatch):
        bootstrap_library_collection("foo", "https://other.example.com")
