from pathlib import Path
import pytest
from lies.library.paths import Library, LibraryCollection, CollectionNameError
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
    with pytest.raises(CollectionNameError, match="invalid collection name"):
        lib.collection("../etc")
    with pytest.raises(CollectionNameError, match="invalid collection name"):
        lib.collection("with space")


@pytest.mark.parametrize(
    "bad_name",
    [
        # Empty string.
        "",
        # Whitespace.
        " ",
        "with space",
        "tab\there",
        # Leading dash (regex requires [a-z0-9] start).
        "-leading-dash",
        # Slash / nested path.
        "with/slash",
        "nested/path",
        # Path traversal.
        "../etc",
        "..",
        ".",
        # Dot in name.
        "with.dot",
        # Uppercase.
        "CamelCase",
        # Too long (regex is 1-64 chars).
        "a" * 65,
        # Starts with non-alphanumeric.
        "_underscore-prefix",
        # Unicode.
        "名前",
    ],
)
def test_collection_name_regex_rejects_invalid_inputs(tmp_xdg: Path, bad_name: str) -> None:
    """Minor 47: parametrized coverage of the full ``_COLLECTION_NAME_RE`` rejection list.

    Replaces the previous two-case (../etc + with space) coverage with
    a parametrized list covering whitespace, leading dash, slash,
    path-traversal, dot-in-name, uppercase, too-long, leading
    underscore, and unicode — every shape the regex rejects.
    """
    lib = Library.open()
    with pytest.raises(CollectionNameError, match="invalid collection name"):
        lib.collection(bad_name)


@pytest.mark.parametrize(
    "good_name",
    [
        "claude",
        "openai",
        "github-pages",  # dash OK in non-leading position
        "scraper_v2",  # underscore OK in non-leading position
        "0digit-start",  # digit OK as leading char
        "x",
        "a" * 64,  # max length
    ],
)
def test_collection_name_regex_accepts_valid_inputs(tmp_xdg: Path, good_name: str) -> None:
    """Minor 47: parametrized coverage of the ``_COLLECTION_NAME_RE`` accept list.

    Pins the positive half: a collection name that matches the regex
    builds successfully. A regression that tightened the regex would
    catch in the negative list above.
    """
    lib = Library.open()
    coll = lib.collection(good_name)
    assert coll.name == good_name


def test_library_collection_direct_instantiation_validates(tmp_xdg: Path) -> None:
    """Minor 40: direct ``LibraryCollection(...)`` construction also validates.

    The previous design routed every construction through
    ``Library.collection(name)`` (which called ``_validate_collection_name``).
    Direct construction ``LibraryCollection(library=lib, name=...)``
    bypassed the validation entirely. The fix moves validation into
    ``__post_init__`` so every construction path enforces the regex.
    """
    lib = Library.open()
    with pytest.raises(CollectionNameError, match="invalid collection name"):
        LibraryCollection(library=lib, name="../etc")
    with pytest.raises(CollectionNameError, match="invalid collection name"):
        LibraryCollection(library=lib, name="with space")
