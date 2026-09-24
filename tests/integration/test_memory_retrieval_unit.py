# tests/unit/memory/test_retrieval.py
from pathlib import Path
from textwrap import dedent

import pytest

from lies.memory.models import WikiSearchResult
from lies.memory.retrieval import read_pages, search_wiki
from tests.conftest import make_wiki


@pytest.fixture
def indexed_wiki(tmp_path: Path):
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
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
    return make_wiki(name="retrieval-test", data_root=root)


def test_search_wiki_falls_back_to_index_when_qmd_missing(
    indexed_wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lies import qmd

    def missing(_cwd: Path, _q: str, _limit: int, **_kw) -> list[dict[str, object]]:
        from lies.qmd.cli import QmdNotInstalledError

        raise QmdNotInstalledError("no qmd")

    monkeypatch.setattr(qmd.cli, "qmd_query", missing)
    result = search_wiki(indexed_wiki, "MVC")
    assert isinstance(result, WikiSearchResult)
    assert result.fallback_used is True
    assert result.fallback_reason == "qmd_unavailable"
    assert result.pages
    assert result.pages[0].path.endswith("concepts/mvc.md")


def test_search_wiki_uses_qmd_when_available(indexed_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    from lies import qmd

    def fake_query(_cwd: Path, _q: str, _limit: int, **_kw) -> list[dict[str, object]]:
        return [
            {
                "path": str(indexed_wiki.data_root / "wiki" / "concepts" / "mvc.md"),
                "score": 0.9,
            }
        ]

    monkeypatch.setattr(qmd.cli, "qmd_query", fake_query)
    result = search_wiki(indexed_wiki, "MVC")
    assert result.fallback_used is False
    assert result.pages[0].path.endswith("concepts/mvc.md")


def test_search_wiki_marks_truncated_when_more_than_limit(
    indexed_wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(8):
        (indexed_wiki.wiki_dir / "concepts" / f"topic_{i}.md").write_text(
            f"---\ntitle: T{i}\ntype: concept\n---\n# T{i}\n", encoding="utf-8"
        )

    def fake_query(_cwd: Path, _q: str, limit: int, **_kw) -> list[dict[str, object]]:
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


def test_read_pages_returns_content_for_ids(indexed_wiki) -> None:
    # Construct a search to assign page IDs.
    def fake_query(_cwd: Path, _q: str, _limit: int, **_kw) -> list[dict[str, object]]:
        return [
            {
                "path": str(indexed_wiki.data_root / "wiki" / "concepts" / "mvc.md"),
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


def test_read_pages_missing_id_returns_empty(indexed_wiki) -> None:
    bodies = read_pages(indexed_wiki, ["not-a-real-id"])
    assert bodies == {}


# ``no_coverage`` flag — F18 Task 2/3 surface. The flag is True when the
# wiki corpus is non-empty AND the search returned zero hits. Three
# matrix cases live below; the closure capture in
# ``src/lies/agents/librarian.py`` picks the value up via
# ``WikiSearchResult.model_dump()``.


def test_search_no_coverage_true_when_corpus_non_empty_and_no_hits(
    indexed_wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wiki has a page but the search returns nothing → no_coverage=True.

    Pins the F18 Task 2 contract: a populated wiki that misses the
    query surfaces ``no_coverage=True`` so the librarian's closure
    capture copies it onto ``LibrarianOutput``. Without the surface,
    ``result.get("no_coverage", False)`` defaults to ``False`` and the
    corpus state is invisible to downstream callers (the bug F18
    Tasks 2/3 fix).
    """
    from lies import qmd

    def missing(_cwd: Path, _q: str, _limit: int, **_kw) -> list[dict[str, object]]:
        from lies.qmd.cli import QmdNotInstalledError

        raise QmdNotInstalledError("no qmd")

    monkeypatch.setattr(qmd.cli, "qmd_query", missing)
    result = search_wiki(indexed_wiki, "asdf-nonexistent")
    assert result.pages == []
    assert result.no_coverage is True


def test_search_no_coverage_false_when_corpus_empty_and_no_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty wiki + zero hits → no_coverage=False.

    No corpus to miss against. The catalog probe returns 0 rows and
    the flag stays False — the failure-open branch in
    ``retrieval._no_coverage_flag`` keeps the contract distinct from
    a populated-but-missed wiki.
    """
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    empty_wiki = make_wiki(name="empty-corpus", data_root=root)

    from lies import qmd

    def missing(_cwd: Path, _q: str, _limit: int, **_kw) -> list[dict[str, object]]:
        from lies.qmd.cli import QmdNotInstalledError

        raise QmdNotInstalledError("no qmd")

    monkeypatch.setattr(qmd.cli, "qmd_query", missing)
    result = search_wiki(empty_wiki, "anything")
    assert result.pages == []
    assert result.no_coverage is False


def test_search_no_coverage_false_when_corpus_non_empty_and_has_hits(
    indexed_wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wiki has a page AND the search lands a hit → no_coverage=False.

    A successful search is never a no-coverage signal regardless of
    the underlying corpus size. Pins the second half of the contract.
    """
    from lies import qmd

    def fake_query(_cwd: Path, _q: str, _limit: int, **_kw) -> list[dict[str, object]]:
        return [
            {
                "path": str(indexed_wiki.data_root / "wiki" / "concepts" / "mvc.md"),
                "score": 0.9,
            }
        ]

    monkeypatch.setattr(qmd.cli, "qmd_query", fake_query)
    result = search_wiki(indexed_wiki, "MVC")
    assert result.pages, "the positive-control hit was dropped — the test is invalid"
    assert result.no_coverage is False


def test_from_qmd_resolves_under_wiki_dir(indexed_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    """After PR #39, qmd is registered at wiki.wiki_dir/<collection>/.

    _from_qmd must join raw_path under wiki.wiki_dir, not wiki.data_root,
    so the relative_to(wiki.wiki_dir) check succeeds and the hit is
    kept (not silently dropped).
    """
    from lies import qmd

    def fake_query(_cwd: Path, _q: str, _limit: int, **_kw) -> list[dict[str, object]]:
        # qmd returns the path *within* the registered root. For a
        # collection named ``concepts`` the relative hit path is
        # ``concepts/mvc.md``; joining it under ``wiki.wiki_dir`` must
        # land on the actual on-disk file.
        return [{"path": "concepts/mvc.md", "score": 0.9}]

    monkeypatch.setattr(qmd.cli, "qmd_query", fake_query)
    result = search_wiki(indexed_wiki, "MVC")
    assert result.fallback_used is False
    assert result.pages, "hit was silently dropped by the wrong join under wiki.data_root"
    assert result.pages[0].path == "concepts/mvc.md"
