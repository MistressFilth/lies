import pytest

pytestmark = pytest.mark.slow  # noqa: E402

from lies.library.bootstrap import bootstrap_library_collection  # noqa: E402
from lies.library.config_io import load_config  # noqa: E402
from lies.library.errors import CollectionMismatch  # noqa: E402
from lies.library.paths import Library  # noqa: E402


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


@pytest.mark.slow
def test_bootstrap_creates_config_when_missing(library: Library) -> None:
    rec = bootstrap_library_collection("foo", "https://example.com/foo.txt")
    assert rec.name == "foo"
    assert rec.source == "https://example.com/foo.txt"
    assert load_config("foo").source == "https://example.com/foo.txt"


@pytest.mark.slow
def test_bootstrap_returns_existing_when_source_matches(library: Library) -> None:
    bootstrap_library_collection("foo", "https://example.com/foo.txt")
    rec = bootstrap_library_collection("foo", "https://example.com/foo.txt")
    assert rec.source == "https://example.com/foo.txt"


def test_bootstrap_raises_mismatch_when_source_differs(library: Library) -> None:
    bootstrap_library_collection("foo", "https://example.com/foo.txt")
    with pytest.raises(CollectionMismatch):
        bootstrap_library_collection("foo", "https://other.example.com")
