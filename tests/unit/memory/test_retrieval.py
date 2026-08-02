# tests/unit/memory/test_retrieval.py
from pathlib import Path
from textwrap import dedent

import pytest

from lies.memory.models import WikiSearchResult
from lies.memory.retrieval import read_pages, search_wiki
from lies.wiki.layout import WikiLayout


@pytest.fixture
def indexed_wiki(tmp_path: Path) -> WikiLayout:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "concepts" / "mvc.md").write_text(
        dedent(
            """\
            ---
            title: Model-View-Controller
            type: concept
            ---
            # Model-View-Controller

            An architectural pattern.
            """
        ),
        encoding="utf-8",
    )
    (root / "wiki" / "index.md").write_text("- [MVC](concepts/mvc.md)\n", encoding="utf-8")
    return WikiLayout(root)


def test_search_wiki_falls_back_to_index_when_qmd_missing(
    indexed_wiki: WikiLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lies import qmd

    def missing(_cwd: Path, _q: str, _limit: int) -> list[dict[str, object]]:
        from lies.qmd.cli import QmdNotInstalledError

        raise QmdNotInstalledError("no qmd")

    monkeypatch.setattr(qmd.cli, "qmd_query", missing)
    result = search_wiki(indexed_wiki, "MVC")
    assert isinstance(result, WikiSearchResult)
    assert result.fallback_used is True
    assert result.fallback_reason == "qmd_unavailable"
    assert result.pages
    assert result.pages[0].path.endswith("concepts/mvc.md")


def test_search_wiki_uses_qmd_when_available(
    indexed_wiki: WikiLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lies import qmd

    def fake_query(_cwd: Path, _q: str, _limit: int) -> list[dict[str, object]]:
        return [
            {
                "path": str(indexed_wiki.root / "wiki" / "concepts" / "mvc.md"),
                "score": 0.9,
            }
        ]

    monkeypatch.setattr(qmd.cli, "qmd_query", fake_query)
    result = search_wiki(indexed_wiki, "MVC")
    assert result.fallback_used is False
    assert result.pages[0].path.endswith("concepts/mvc.md")


def test_search_wiki_marks_truncated_when_more_than_limit(
    indexed_wiki: WikiLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(8):
        (indexed_wiki.wiki_dir / "concepts" / f"topic_{i}.md").write_text(
            f"---\ntitle: T{i}\ntype: concept\n---\n# T{i}\n", encoding="utf-8"
        )

    def fake_query(_cwd: Path, _q: str, limit: int) -> list[dict[str, object]]:
        paths = [
            str(indexed_wiki.wiki_dir / "concepts" / f"topic_{i}.md")
            for i in range(min(limit + 1, 8))
        ]
        return [{"path": p, "score": 1.0 - 0.01 * i} for i, p in enumerate(paths)]

    from lies import qmd

    monkeypatch.setattr(qmd.cli, "qmd_query", fake_query)
    result = search_wiki(indexed_wiki, "topic", limit=3)
    assert result.truncated is True
    assert len(result.pages) == 3


def test_read_pages_returns_content_for_ids(indexed_wiki: WikiLayout) -> None:
    # Construct a search to assign page IDs.
    def fake_query(_cwd: Path, _q: str, _limit: int) -> list[dict[str, object]]:
        return [
            {
                "path": str(indexed_wiki.root / "wiki" / "concepts" / "mvc.md"),
                "score": 0.9,
            }
        ]

    from lies import qmd as _qmd_mod

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(_qmd_mod.cli, "qmd_query", fake_query)
        result = search_wiki(indexed_wiki, "MVC")
    finally:
        monkeypatch.undo()
    page_id = result.pages[0].page_id
    bodies = read_pages(indexed_wiki, [page_id])
    assert page_id in bodies
    assert "Model-View-Controller" in bodies[page_id]


def test_read_pages_missing_id_returns_empty(indexed_wiki: WikiLayout) -> None:
    bodies = read_pages(indexed_wiki, ["not-a-real-id"])
    assert bodies == {}
