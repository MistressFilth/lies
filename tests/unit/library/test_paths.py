from pathlib import Path
import pytest
from lies.library.paths import Library
from lies import xdg
from lies.constants import LIES_DATA_SUBDIR


@pytest.fixture
def tmp_xdg(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    return tmp_path


def test_library_open_returns_singleton(tmp_xdg: Path) -> None:
    a = Library.open()
    b = Library.open()
    assert a is b


def test_library_roots_under_xdg(tmp_xdg: Path) -> None:
    lib = Library.open()
    assert lib.collections_root == tmp_xdg / LIES_DATA_SUBDIR / "library" / "collections"
    assert lib.catalog_path == tmp_xdg / LIES_DATA_SUBDIR / "library" / ".lies" / "catalog.db"
    assert lib.log_path == tmp_xdg / LIES_DATA_SUBDIR / "library" / "log.md"
    assert lib.poison_root == tmp_xdg / LIES_DATA_SUBDIR / "library" / "poison"
    assert lib.git_root == tmp_xdg / LIES_DATA_SUBDIR / "library"


def test_collection_subdirs(tmp_xdg: Path) -> None:
    lib = Library.open()
    coll = lib.collection("claude")
    assert coll.name == "claude"
    assert coll.dir == lib.collections_root / "claude"
    assert coll.raw_dir == coll.dir / "raw"
    assert coll.manifest_path == coll.dir / ".lies" / "manifest.json"


def test_collection_rejects_path_traversal(tmp_xdg: Path) -> None:
    lib = Library.open()
    with pytest.raises(ValueError, match="invalid collection name"):
        lib.collection("../etc")
    with pytest.raises(ValueError, match="invalid collection name"):
        lib.collection("with space")
