"""Pins that ``sync_collection`` writes to library, not wiki.

Mocks the scraper layer so the test does not shell out; asserts the
mirror file lands under ``library.collections_root`` and the wiki's
``wiki_dir`` is untouched.
"""

from pathlib import Path
import subprocess
import pytest
from lies.etl.sync_helper import sync_collection
from lies.library.ingest import BatchIngestResult
from lies.library.paths import Library


@pytest.fixture
def fixture_lib(tmp_path: Path, monkeypatch) -> Library:
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".lies").mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("")
    subprocess.run(
        ["git", "init", "-b", "main", str(lib.git_root)], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(lib.git_root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(lib.git_root), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(lib.git_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "init"], check=True, capture_output=True
    )
    return lib


def _stub_wiki(fixture_lib: Library):
    from lies.wiki.wiki import Wiki

    return Wiki(
        name="t",
        data_root=fixture_lib.git_root,
        config_root=fixture_lib.git_root,
        cache_root=fixture_lib.git_root,
        state_root=fixture_lib.git_root,
        runtime_root=fixture_lib.git_root,
    )


def _seed_collection(wiki, *, name: str = "claude", scraper_cmd: str | None = None) -> None:
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    scraper_line = f"scraper_cmd: {scraper_cmd}\n" if scraper_cmd else ""
    (wiki.collections_dir / f"{name}.yaml").write_text(
        "name: {name}\n"
        "path: raw/{name}\n"
        "source: https://example.com/{name}\n"
        "tags: []\n"
        "{scraper}".format(name=name, scraper=scraper_line)
        + "version: '1'\n"
        "created_at: 2026-01-01T00:00:00\n"
        "updated_at: 2026-01-01T00:00:00\n",
        encoding="utf-8",
    )


def test_sync_collection_writes_to_library(fixture_lib: Library, monkeypatch) -> None:
    """``sync_collection`` lands the mirror under library, not wiki.

    Regression pin for the Phase-2 ingest → library swap. Mocks the
    fetcher layer (so the test does not shell out / hit the network)
    but lets ``run_batch_ingest`` run end-to-end on the real on-disk
    library fixture. Asserts the three contract pins:

      1. Mirror file lands at ``<library.collections_root>/<c>/<slug>.md``.
      2. Library catalog row exists at ``<library.catalog_path>`` with
         ``section="library"``.
      3. Wiki's ``wiki_dir`` did NOT receive a write (negative control).

    Without #1, the helper would silently drop the body. Without #2, the
    visible-memory layer would diverge from disk. Without #3, this test
    could not distinguish a library-write from a leftover wiki-write
    that the operator might still query against.
    """
    from lies.library.catalog import list_pages, open_catalog
    from lies.library.ingest import FetchItem

    wiki = _stub_wiki(fixture_lib)
    _seed_collection(wiki)

    body = (
        "# Hello\n"
        "body line one\n"
        "body line two\n"
        "body line three\n"
        "body line four\n"
        "body line five\n"
        "body line six\n"
        "body line seven\n"
    )

    class _StaticFetcher:
        """Test double: yields one ``FetchItem`` with a body that clears
        the thin-content gate (``should_skip_content``'s ``<= 5`` rule).
        """

        def __init__(self, library, **kwargs) -> None:
            self._library = library

        def fetch_sources(self, source):
            yield FetchItem(
                path=Path("/src/x.md"),
                url=None,
                body=body,
                source_hash="abc123",
                fetched_via="static",
            )

    monkeypatch.setattr("lies.etl.sync_helper.ScraperFetcher", _StaticFetcher)

    result = sync_collection(
        wiki=wiki,
        collection_name="claude",
        force=False,
    )

    coll_dir = fixture_lib.collections_root / "claude"
    # Pin 1: mirror file lands at the library path.
    assert (coll_dir / "x.md").exists(), (
        f"expected mirror at {coll_dir / 'x.md'}; got {list(coll_dir.iterdir())!r}"
    )

    # Pin 2: library catalog row exists with section="library".
    conn = open_catalog(fixture_lib)
    try:
        pages = list_pages(conn, section="library")
    finally:
        conn.close()
    library_slugs = {p.slug for p in pages}
    assert "claude/x" in library_slugs, (
        f"expected slug 'claude/x' in section='library'; got {library_slugs!r}"
    )

    # Pin 3: wiki did NOT receive a write.
    assert not (wiki.wiki_dir / "claude" / "x.md").exists(), (
        "wiki.wiki_dir must not receive a write after Phase-2 retargeting"
    )

    # Contract: ``sync_collection`` surfaces ``BatchIngestResult`` so the
    # CLI can exit non-zero on errors instead of silently swallowing the
    # failure (Task 11's Finding 3).
    assert isinstance(result, BatchIngestResult)
    assert result.created == 1
    assert result.errors == 0


def test_sync_collection_threads_scraper_cmd_into_fetcher(
    fixture_lib: Library, monkeypatch
) -> None:
    """``Collection.scraper_cmd`` is passed through to ``ScraperFetcher``.

    Finding 1 pin: the bespoke loader must be honored end-to-end. We
    capture the ``ScraperFetcher`` instance the helper hands to
    ``run_batch_ingest`` and assert it carries the right
    ``scraper_cmd`` / ``collection`` (REGISTRY routing needs the
    collection for ``Collection.config`` lookups in sphinx / liquid /
    bespoke builders).
    """
    wiki = _stub_wiki(fixture_lib)
    _seed_collection(wiki, scraper_cmd="lies.scrapers.web:WebScraper")

    seen = {}

    class _FakeFetcher:
        def __init__(self, library, **kwargs):
            seen["init"] = {"library": library, **kwargs}

    def fake_run_batch(*args, **kwargs):
        seen["kwargs"] = kwargs
        return BatchIngestResult()

    # Patch the binding the helper actually uses, not the source module.
    monkeypatch.setattr("lies.etl.sync_helper.ScraperFetcher", _FakeFetcher)
    monkeypatch.setattr("lies.library.ingest.run_batch_ingest", fake_run_batch)

    sync_collection(wiki=wiki, collection_name="claude", force=False)

    init = seen["init"]
    assert init["library"] is fixture_lib
    assert init["scraper_cmd"] == "lies.scrapers.web:WebScraper"
    # Collection must be threaded through for REGISTRY builders.
    assert init["collection"] is not None
    assert init["collection"].name == "claude"


def test_sync_collection_no_scraper_cmd_uses_pick_scraper(
    fixture_lib: Library, monkeypatch
) -> None:
    """Without ``scraper_cmd`` the fetcher is built with ``scraper_cmd=None``."""
    wiki = _stub_wiki(fixture_lib)
    _seed_collection(wiki)

    seen = {}

    class _FakeFetcher:
        def __init__(self, library, **kwargs):
            seen["init"] = {"library": library, **kwargs}

    monkeypatch.setattr("lies.etl.sync_helper.ScraperFetcher", _FakeFetcher)
    monkeypatch.setattr(
        "lies.library.ingest.run_batch_ingest", lambda *a, **kw: BatchIngestResult()
    )

    sync_collection(wiki=wiki, collection_name="claude", force=False)

    assert seen["init"]["scraper_cmd"] is None
    assert seen["init"]["collection"].name == "claude"


def test_sync_collection_propagates_errors(fixture_lib: Library, monkeypatch) -> None:
    """A wholly-failed batch surfaces ``errors`` on the returned result.

    Finding 3 pin: the returned ``BatchIngestResult.errors`` is the
    signal the CLI uses to exit non-zero.
    """
    wiki = _stub_wiki(fixture_lib)
    _seed_collection(wiki)

    def fake_run_batch(*args, **kwargs):
        return BatchIngestResult(errors=2, quarantine_records=[("x:u", "broken")])

    monkeypatch.setattr("lies.library.ingest.run_batch_ingest", fake_run_batch)

    result = sync_collection(wiki=wiki, collection_name="claude", force=False)
    assert result.errors == 2
    assert len(result.quarantine_records) == 1
