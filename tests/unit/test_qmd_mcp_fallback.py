from __future__ import annotations

from pathlib import Path

import pytest

from lies.qmd.mcp_fallback import QmdFallbackMcp
from lies.wiki.wiki import Wiki

from tests.conftest import make_wiki


@pytest.fixture
def wiki_root(tmp_path: Path) -> Wiki:
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki").mkdir()
    return make_wiki(name="qmd-fallback", data_root=tmp_path)


@pytest.fixture(autouse=True)
def _stub_qmd_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``qmd_query`` so search tests don't spawn a real qmd subprocess.

    Every test in this module exercises the in-process fallback search
    path; the underlying :func:`WikiMemoryService.search` resolves
    ``qmd_query`` lazily via :mod:`lies.qmd.cli`, so monkeypatching
    that import path covers every entry point.
    """
    monkeypatch.setattr(
        "lies.qmd.cli.qmd_query",
        lambda *a, **kw: [{"path": "concepts/alpha.md", "score": 1.0}],
    )


def test_fallback_lists_expected_tools(wiki_root: Wiki) -> None:
    mcp = QmdFallbackMcp(wiki_root)
    assert set(mcp.tools_known_to_model()) == {"wiki_search", "wiki_read"}


def test_fallback_search_returns_degraded_true(wiki_root: Wiki) -> None:
    """A search call returns bounded evidence and flags it as degraded."""
    from lies.memory.service import WikiMemoryService

    (wiki_root.wiki_dir / "concepts").mkdir()
    (wiki_root.wiki_dir / "concepts" / "alpha.md").write_text(
        "# alpha\n\nDiscusses widgets.", encoding="utf-8"
    )
    (wiki_root.wiki_dir / "index.md").write_text(
        "# Index\n\n- [alpha](concepts/alpha.md)\n", encoding="utf-8"
    )
    mcp = QmdFallbackMcp(wiki_root)
    service = WikiMemoryService(wiki=wiki_root)

    raw = mcp.call_wiki_search(service, question="widgets", limit=5)
    payload = raw.model_dump()
    assert payload["degraded"] is True
    assert payload["pages"]


def test_fallback_read_returns_page_body(wiki_root: Wiki) -> None:
    from lies.memory.retrieval import _page_id_for, _path_for_id
    from lies.memory.service import WikiMemoryService

    (wiki_root.wiki_dir / "concepts").mkdir()
    (wiki_root.wiki_dir / "concepts" / "alpha.md").write_text(
        "# alpha\n\nWidgets widget widgets.\n", encoding="utf-8"
    )
    mcp = QmdFallbackMcp(wiki_root)
    service = WikiMemoryService(wiki=wiki_root)

    page_id = _page_id_for("concepts/alpha.md")
    assert _path_for_id(wiki_root, page_id) == "concepts/alpha.md"
    result = mcp.call_wiki_read(service, page_ids=[page_id])
    assert page_id in result
    assert "Widgets" in result[page_id]


def test_fallback_read_rejects_unknown_ids(wiki_root: Wiki) -> None:
    from lies.memory.models import WikiPageNotFound
    from lies.memory.service import WikiMemoryService

    mcp = QmdFallbackMcp(wiki_root)
    service = WikiMemoryService(wiki=wiki_root)
    with pytest.raises(WikiPageNotFound):
        mcp.call_wiki_read(service, page_ids=["page-deadbeefdeadbeef"])


def test_fallback_search_with_empty_wiki_is_empty_and_degraded(
    wiki_root: Wiki,
) -> None:
    from lies.memory.service import WikiMemoryService

    mcp = QmdFallbackMcp(wiki_root)
    service = WikiMemoryService(wiki=wiki_root)
    raw = mcp.call_wiki_search(service, question="anything", limit=5)
    payload = raw.model_dump()
    assert payload["degraded"] is True
    assert payload["pages"] == []
