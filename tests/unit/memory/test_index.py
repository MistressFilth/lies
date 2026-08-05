from pathlib import Path

import pytest

from lies.memory.index import append_log_entry, rebuild_index
from tests.conftest import make_wiki


@pytest.fixture
def seeded_wiki(tmp_path: Path):
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
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
    return make_wiki(name="seeded", data_root=root)


def test_rebuild_index_lists_pages_with_titles(seeded_wiki) -> None:
    rebuild_index(seeded_wiki)
    text = (seeded_wiki.wiki_dir / "index.md").read_text(encoding="utf-8")
    assert "# Index" in text
    assert "X" in text
    assert "Y" in text


def test_rebuild_index_groups_by_page_type(seeded_wiki) -> None:
    rebuild_index(seeded_wiki)
    text = (seeded_wiki.wiki_dir / "index.md").read_text(encoding="utf-8")
    # "concepts" appears before "entities" because the index is grouped.
    assert text.find("concepts") < text.find("entities")


def test_append_log_entry_creates_file(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    wiki = make_wiki(name="log1", data_root=root)
    append_log_entry(wiki, "## [2026-07-29] ingest | A")
    log_path = wiki.wiki_dir / "log.md"
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert "## [2026-07-29] ingest | A" in text


def test_append_log_entry_appends(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    wiki = make_wiki(name="log2", data_root=root)
    append_log_entry(wiki, "## [2026-07-29] ingest | A")
    append_log_entry(wiki, "## [2026-07-29] query  | B")
    text = (wiki.wiki_dir / "log.md").read_text(encoding="utf-8")
    assert text.count("## [2026-07-29]") == 2
