"""Tests for the etl WRITE stage's bulk catalog update."""

from __future__ import annotations

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
