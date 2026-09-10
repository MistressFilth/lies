"""Unit-level regression pins for ``lies.etl.sync_helper.sync_collection``.

Phase-2 inverted the ingest target from ``wiki.wiki_dir`` to the
library singleton at ``xdg.data_home() / LIES_DATA_SUBDIR / library``.
These tests pin that swap at the ``sync_helper`` boundary so a future
re-routing regression fails closed (file lands at wiki, not library;
catalog row missing in library; negative control violated).

The tests drive ``sync_collection`` end-to-end with a static
``ScraperFetcher`` double so the file lands on real disk and the
catalog row lands in a real ``catalog.db`` — the assertions then
inspect those artifacts directly.

The wiki fixture is a stub whose ``data_root`` shadows the library's
``git_root`` so the negative control (no write to
``wiki.wiki_dir / <c> / <slug>.md``) is meaningful: a regression that
re-targets the write back to ``wiki.wiki_dir`` would re-introduce the
file at the negative-control path and trip the assertion.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from lies.etl.sync_helper import sync_collection
from lies.library.catalog import list_pages, open_catalog
from lies.library.ingest import BatchIngestResult, FetchItem
from lies.library.paths import Library


_BODY = (
    "# Hello\n"
    "body line one\n"
    "body line two\n"
    "body line three\n"
    "body line four\n"
    "body line five\n"
    "body line six\n"
    "body line seven\n"
)


@pytest.fixture
def fixture_lib(tmp_path: Path, monkeypatch) -> Library:
    """Library singleton rooted at ``tmp_path/.lies/library`` (XDG-routed).

    Mirrors ``tests/integration/test_sync_library.py::fixture_lib`` so
    the wiki stub can resolve ``xdg.data_home()`` through the same
    monkeypatched function. ``.gitkeep`` keeps ``git add .`` non-empty
    so the initial commit lands.
    """
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".lies").mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("")
    subprocess.run(
        ["git", "init", "-b", "main", str(lib.git_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.name", "t"],
        check=True,
    )
    subprocess.run(["git", "-C", str(lib.git_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return lib


def _stub_wiki(fixture_lib: Library):
    """Wiki whose roots shadow ``fixture_lib.git_root``.

    Same pattern as the integration-tier pin: the wiki shares the
    library's git dir so a regression that re-targets the write back
    to ``wiki.wiki_dir`` (NOT the library) would surface at the
    negative-control assertion because the layout would no longer be
    disjoint.
    """
    from lies.wiki.wiki import Wiki

    return Wiki(
        name="t",
        data_root=fixture_lib.git_root,
        config_root=fixture_lib.git_root,
        cache_root=fixture_lib.git_root,
        state_root=fixture_lib.git_root,
        runtime_root=fixture_lib.git_root,
    )


def _seed_collection(wiki, *, name: str = "claude") -> None:
    """Drop a minimal collection YAML that ``load_collection`` can read."""
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / f"{name}.yaml").write_text(
        "name: {name}\n"
        "path: raw/{name}\n"
        "source: https://example.com/{name}\n"
        "tags: []\n"
        "version: '1'\n"
        "created_at: 2026-01-01T00:00:00\n"
        "updated_at: 2026-01-01T00:00:00\n".format(name=name),
        encoding="utf-8",
    )


class _StaticFetcher:
    """Test double: yields one ``FetchItem`` with a body that clears the
    thin-content gate (``should_skip_content``'s ``<= 5`` rule)."""

    def __init__(self, library, **kwargs) -> None:
        self._library = library

    def fetch_sources(self, source):
        yield FetchItem(
            path=Path("/src/x.md"),
            url=None,
            body=_BODY,
            source_hash="abc123",
            fetched_via="static",
        )


def test_sync_helper_writes_to_library_not_wiki(fixture_lib: Library, monkeypatch) -> None:
    """``sync_helper.sync_collection`` lands the mirror at the library path.

    Three contract pins:

      1. Mirror file lands at ``<library.collections_root>/<c>/<slug>.md``.
      2. Catalog row exists in ``<library.catalog_path>`` with
         ``section="library"``.
      3. Wiki's ``wiki_dir`` did NOT receive a write (negative control).

    Without all three, the ingest → library swap is not actually
    effective on disk. A regression that writes to one location but
    leaves the other unchanged would break at least one pin.
    """
    wiki = _stub_wiki(fixture_lib)
    _seed_collection(wiki)

    monkeypatch.setattr("lies.etl.sync_helper.ScraperFetcher", _StaticFetcher)

    result = sync_collection(
        wiki=wiki,
        collection_name="claude",
        force=False,
    )

    # Pin 1: mirror file lands at the library path.
    coll_dir = fixture_lib.collections_root / "claude"
    assert (coll_dir / "x.md").exists(), (
        f"expected mirror at {coll_dir / 'x.md'}; "
        f"got {sorted(p.name for p in coll_dir.iterdir())!r}"
    )

    # Pin 2: catalog row exists with section="library".
    conn = open_catalog(fixture_lib)
    try:
        pages = list_pages(conn, section="library")
    finally:
        conn.close()
    library_slugs = {p.slug for p in pages}
    assert "claude/x" in library_slugs, (
        f"expected slug 'claude/x' in section='library'; got {library_slugs!r}"
    )

    # Pin 3: wiki did NOT receive a write. Negative control — a
    # regression that re-targets ``sync_collection`` back to the wiki
    # would surface here.
    assert not (wiki.wiki_dir / "claude" / "x.md").exists(), (
        "wiki.wiki_dir must not receive a write after Phase-2 retargeting; "
        "sync_collection should land the mirror at the library singleton"
    )

    # Contract: ``sync_collection`` surfaces ``BatchIngestResult`` so the
    # CLI can exit non-zero on errors instead of silently swallowing the
    # failure.
    assert isinstance(result, BatchIngestResult)
    assert result.created == 1
    assert result.errors == 0


def test_sync_helper_returns_batch_ingest_result_from_library(
    fixture_lib: Library, monkeypatch
) -> None:
    """The library ``run_batch_ingest`` is the one whose return value flows
    back out of ``sync_collection``.

    Without this pin, a regression that routes the call back through
    the legacy wiki-side ``run_write`` (which returns ``StageResult``,
    not ``BatchIngestResult``) would explode at the ``CLI`` exit-code
    branch — or worse, silently exit 0 on a wholly-failed batch.
    Mocks the library-side ``run_batch_ingest`` so the test does not
    depend on the real ingest pipeline; captures the kwargs to assert
    ``library`` is plumbed through.
    """
    wiki = _stub_wiki(fixture_lib)
    _seed_collection(wiki)

    captured: dict[str, object] = {}

    def fake_run_batch(*args, **kwargs):
        captured.update(kwargs)
        return BatchIngestResult(created=1)

    monkeypatch.setattr("lies.library.ingest.run_batch_ingest", fake_run_batch)

    result = sync_collection(
        wiki=wiki,
        collection_name="claude",
        force=False,
    )

    # Library is the write target — must be plumbed through to
    # ``run_batch_ingest`` so the mirror lands under
    # ``library.collections_root`` (Phase-2 retargeting).
    assert captured.get("library") is fixture_lib
    # Collection name flows through verbatim.
    assert captured.get("collection_name") == "claude"
    # Returned ``BatchIngestResult`` is the one the caller sees, so
    # the CLI's ``result.errors > 0 → exit 1`` contract (Task 11
    # Finding 3) can fire.
    assert isinstance(result, BatchIngestResult)
    assert result.created == 1
    assert result.errors == 0
