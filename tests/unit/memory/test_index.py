from pathlib import Path

import pytest

from lies.memory.index import append_log_entry, rebuild_index
from lies.wiki.layout import WikiLayout


@pytest.fixture
def seeded_wiki(tmp_path: Path) -> WikiLayout:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "entities").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    (root / "wiki" / "concepts" / "x.md").write_text(
        "---\ntitle: X\ntype: concept\n---\n# X\n", encoding="utf-8"
    )
    (root / "wiki" / "entities" / "y.md").write_text(
        "---\ntitle: Y\ntype: entity\n---\n# Y\n", encoding="utf-8"
    )
    return WikiLayout(root)


def test_rebuild_index_lists_pages_with_titles(seeded_wiki: WikiLayout) -> None:
    rebuild_index(seeded_wiki)
    text = seeded_wiki.index_path.read_text(encoding="utf-8")
    assert "# Index" in text
    assert "X" in text
    assert "Y" in text


def test_rebuild_index_groups_by_page_type(seeded_wiki: WikiLayout) -> None:
    rebuild_index(seeded_wiki)
    text = seeded_wiki.index_path.read_text(encoding="utf-8")
    # "concepts" appears before "entities" because the index is grouped.
    assert text.find("concepts") < text.find("entities")


def test_append_log_entry_creates_file(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    layout = WikiLayout(root)
    append_log_entry(layout, "## [2026-07-29] ingest | A")
    assert layout.log_path.exists()
    text = layout.log_path.read_text(encoding="utf-8")
    assert "## [2026-07-29] ingest | A" in text


def test_append_log_entry_appends(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    layout = WikiLayout(root)
    append_log_entry(layout, "## [2026-07-29] ingest | A")
    append_log_entry(layout, "## [2026-07-29] query  | B")
    text = layout.log_path.read_text(encoding="utf-8")
    assert text.count("## [2026-07-29]") == 2
