from __future__ import annotations

from pathlib import Path

import pytest

from lies.qmd.mcp_fallback import QmdFallbackMcp
from lies.wiki.layout import WikiLayout


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".lies").mkdir()
    return tmp_path


def test_fallback_lists_expected_tools(wiki_root: Path) -> None:
    layout = WikiLayout(wiki_root)
    mcp = QmdFallbackMcp(layout)
    assert set(mcp.tools_known_to_model()) == {"wiki_search", "wiki_read"}


def test_fallback_search_returns_degraded_true(wiki_root: Path) -> None:
    """A search call returns bounded evidence and flags it as degraded."""
    from lies.memory.service import WikiMemoryService

    (wiki_root / "wiki" / "concepts").mkdir()
    (wiki_root / "wiki" / "concepts" / "alpha.md").write_text(
        "# alpha\n\nDiscusses widgets.", encoding="utf-8"
    )
    (wiki_root / "wiki" / "index.md").write_text(
        "# Index\n\n- [alpha](concepts/alpha.md)\n", encoding="utf-8"
    )
    layout = WikiLayout(wiki_root)
    mcp = QmdFallbackMcp(layout)
    service = WikiMemoryService(layout)

    raw = mcp.call_wiki_search(service, question="widgets", limit=5)
    payload = raw.model_dump()
    assert payload["degraded"] is True
    assert payload["pages"]


def test_fallback_read_returns_page_body(wiki_root: Path) -> None:
    from lies.memory.retrieval import _page_id_for, _path_for_id
    from lies.memory.service import WikiMemoryService

    (wiki_root / "wiki" / "concepts").mkdir()
    (wiki_root / "wiki" / "concepts" / "alpha.md").write_text(
        "# alpha\n\nWidgets widget widgets.\n", encoding="utf-8"
    )
    layout = WikiLayout(wiki_root)
    mcp = QmdFallbackMcp(layout)
    service = WikiMemoryService(layout)

    page_id = _page_id_for("concepts/alpha.md")
    assert _path_for_id(layout, page_id) == "concepts/alpha.md"
    result = mcp.call_wiki_read(service, page_ids=[page_id])
    assert page_id in result
    assert "Widgets" in result[page_id]


def test_fallback_read_rejects_unknown_ids(wiki_root: Path) -> None:
    from lies.memory.models import WikiPageNotFound
    from lies.memory.service import WikiMemoryService

    layout = WikiLayout(wiki_root)
    mcp = QmdFallbackMcp(layout)
    service = WikiMemoryService(layout)
    with pytest.raises(WikiPageNotFound):
        mcp.call_wiki_read(service, page_ids=["page-deadbeefdeadbeef"])


def test_fallback_search_with_empty_wiki_is_empty_and_degraded(
    wiki_root: Path,
) -> None:
    from lies.memory.service import WikiMemoryService

    layout = WikiLayout(wiki_root)
    mcp = QmdFallbackMcp(layout)
    service = WikiMemoryService(layout)
    raw = mcp.call_wiki_search(service, question="anything", limit=5)
    payload = raw.model_dump()
    assert payload["degraded"] is True
    assert payload["pages"] == []
