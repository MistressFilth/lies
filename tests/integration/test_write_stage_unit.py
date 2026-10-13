"""Tests for the etl WRITE stage's bulk catalog update."""

from __future__ import annotations

import subprocess
from pathlib import Path

from lies.memory.catalog import list_pages, open_catalog
from lies.etl.stages.write import _bulk_update_catalog  # noqa: F401 — under test


def test_bulk_update_catalog_upserts_written_paths(tmp_path: Path) -> None:
    """After WRITE stage writes N files, _bulk_update_catalog upserts all N rows."""
    wiki_root = tmp_path / "test-wiki"
    wiki_root.mkdir()
    collection_dir = wiki_root / "wiki" / "claude-code"
    collection_dir.mkdir(parents=True)
    for i in range(3):
        (collection_dir / f"page-{i}.md").write_text(
            f"---\ntitle: Page {i}\n---\n\n# Page {i}\n",
            encoding="utf-8",
        )

    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = wiki_root  # type: ignore[attr-defined]

    _bulk_update_catalog(
        wiki,
        ["claude-code/page-0", "claude-code/page-1", "claude-code/page-2"],
    )

    conn = open_catalog(wiki)
    try:
        pages = list_pages(conn)
        slugs = {p.slug for p in pages}
    finally:
        conn.close()
    assert slugs == {"claude-code/page-0", "claude-code/page-1", "claude-code/page-2"}
    for p in pages:
        assert p.source_pkg == "claude-code"


def test_bulk_update_catalog_empty_paths(tmp_path: Path) -> None:
    """Empty path list is a no-op (does not raise)."""

    class _StubWiki:
        pass

    wiki = _StubWiki()
    wiki.wiki_dir = tmp_path  # type: ignore[attr-defined]

    _bulk_update_catalog(wiki, [])  # no raise


def test_library_write_stage_upserts_catalog_rows(tmp_path: Path, monkeypatch) -> None:
    """Phase-2 inversion pin: library WRITE stage upserts catalog rows."""
    from lies.library.catalog import (
        list_pages as list_library_pages,
        open_catalog as open_library_catalog,
    )
    from lies.library.ingest import FetchItem, run_batch_ingest
    from lies.library.paths import Library

    # XDG-isolate the library under ``tmp_path/.lies/library``.
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

    # Drop the qmd post-commit hook so the test does not depend on a
    # running qmd daemon — mirrors the wiki-side WRITE stage contract
    # where qmd failures are non-fatal stderr warnings.
    monkeypatch.setattr(
        "lies.library.writer.qmd_collection_add_or_update",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr("lies.library.writer.qmd_update", lambda *a, **kw: None)
    monkeypatch.setattr("lies.library.writer.qmd_embed", lambda *a, **kw: None)

    body = (
        "# Page 0\n"
        "body line one\n"
        "body line two\n"
        "body line three\n"
        "body line four\n"
        "body line five\n"
        "body line six\n"
        "body line seven\n"
    )

    class _StaticFetcher:
        """Yields one ``FetchItem`` whose body clears the thin-content
        gate. Slug derives from ``item.path.stem`` so ``page-0``
        produces slug ``claude-code/page-0`` in the library catalog —
        matching the wiki-side slug shape (see
        ``test_bulk_update_catalog_upserts_written_paths`` above)."""

        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch_sources(self, source):
            for i in range(3):
                yield FetchItem(
                    path=Path(f"/src/page-{i}.md"),
                    url=None,
                    body=body,
                    source_hash=f"hash{i}",
                    fetched_via="static",
                )

    result = run_batch_ingest(
        library=lib,
        collection_name="claude-code",
        source_dir="https://example.com/claude-code",
        fetcher=_StaticFetcher(),
        force=False,
    )

    # Three mirrors landed at the library path.
    assert result.created == 3
    assert result.errors == 0

    coll_dir = lib.collections_root / "claude-code"
    assert (coll_dir / "page-0.md").exists()
    assert (coll_dir / "page-1.md").exists()
    assert (coll_dir / "page-2.md").exists()

    # The library catalog has all three rows with the right shape.
    # Pin: ``section='library'`` — a regression to ``'wiki'`` or
    # ``'ingested'`` would break the catalog's CHECK constraint
    # (``lies/library/catalog.py:_DDL``) AND would surface as a row
    # in the wrong catalog on a real query.
    conn = open_library_catalog(lib)
    try:
        rows = list_library_pages(conn, section="library", source_pkg="claude-code")
    finally:
        conn.close()
    assert {p.slug for p in rows} == {
        "claude-code/page-0",
        "claude-code/page-1",
        "claude-code/page-2",
    }
    for p in rows:
        assert p.section == "library"
        assert p.source_pkg == "claude-code"
