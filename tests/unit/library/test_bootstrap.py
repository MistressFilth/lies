import pytest

pytestmark = pytest.mark.slow  # noqa: E402

from lies.library.bootstrap import (  # noqa: E402
    BootstrapAllReport,
    bootstrap_all_missing_configs,
    bootstrap_library_collection,
)
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


@pytest.fixture(autouse=True)
def _clear_library_collection_name_cache():
    """Each test gets a fresh ``library_collection_names`` snapshot.

    The registry memoizes the directory listing for the process; without
    this fixture, a test that mutates ``collections_root`` would inherit
    a stale snapshot from an earlier test in the same xdist worker.
    """
    from lies.library.registry import (
        _library_collection_names_cached,
        _library_collection_tags_cached,
    )

    _library_collection_names_cached.cache_clear()
    _library_collection_tags_cached.cache_clear()
    yield
    _library_collection_names_cached.cache_clear()
    _library_collection_tags_cached.cache_clear()


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


def test_bootstrap_all_creates_only_missing_configs(library: Library) -> None:
    """Sweep writes configs for no-config dirs and skips ones that have one.

    Three collection directories on disk (one pre-bootstrapped, two
    scraped without a config). The sweep must write the two missing
    configs, leave the existing one alone, and report counts that match.
    """
    # Pre-existing config: must NOT be overwritten by the sweep.
    bootstrap_library_collection("has-config", "https://real.example.com/has-config")
    # Two scraped-only dirs (no config.yaml).
    (library.collections_root / "needs-config-a").mkdir()
    (library.collections_root / "needs-config-a" / "page.md").write_text("hi", encoding="utf-8")
    (library.collections_root / "needs-config-b").mkdir()
    (library.collections_root / "needs-config-b" / "page.md").write_text("hi", encoding="utf-8")

    report = bootstrap_all_missing_configs()

    assert isinstance(report, BootstrapAllReport)
    assert sorted(report.created) == ["needs-config-a", "needs-config-b"]
    assert report.skipped == ("has-config",)
    assert report.skipped_sources == {"has-config": "https://real.example.com/has-config"}

    # Pre-existing config's source was preserved.
    assert load_config("has-config").source == "https://real.example.com/has-config"
    # New configs carry the placeholder source and are loadable.
    assert load_config("needs-config-a").source == "bootstrap-all://needs-config-a"
    assert load_config("needs-config-b").source == "bootstrap-all://needs-config-b"


def test_bootstrap_all_is_idempotent(library: Library) -> None:
    """A second run on the same state creates nothing new.

    Re-running the sweep is a no-op: every collection now has a config
    (either pre-existing or written by the first sweep), so nothing
    needs bootstrapping. The skipped counts grow to cover them all.
    """
    (library.collections_root / "alpha").mkdir()
    (library.collections_root / "beta").mkdir()

    first = bootstrap_all_missing_configs()
    second = bootstrap_all_missing_configs()

    assert sorted(first.created) == ["alpha", "beta"]
    assert second.created == ()
    assert sorted(second.skipped) == ["alpha", "beta"]
    assert second.skipped_sources["alpha"] == "bootstrap-all://alpha"
    assert second.skipped_sources["beta"] == "bootstrap-all://beta"


def test_bootstrap_all_empty_library_reports_no_creates(library: Library) -> None:
    """No collections on disk → empty report, not an error."""
    report = bootstrap_all_missing_configs()
    assert report.created == ()
    assert report.skipped == ()
    assert report.skipped_sources == {}
