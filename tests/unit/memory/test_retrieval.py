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

    def missing(_cwd: Path, _q: str, _limit: int, **kwargs: object) -> list[dict[str, object]]:
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

    def fake_query(_cwd: Path, _q: str, _limit: int, **kwargs: object) -> list[dict[str, object]]:
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

    def fake_query(_cwd: Path, _q: str, limit: int, **kwargs: object) -> list[dict[str, object]]:
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
    def fake_query(_cwd: Path, _q: str, _limit: int, **kwargs: object) -> list[dict[str, object]]:
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

    def missing(_cwd: Path, _q: str, _limit: int, **kwargs: object) -> list[dict[str, object]]:
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

    def missing(_cwd: Path, _q: str, _limit: int, **kwargs: object) -> list[dict[str, object]]:
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

    def fake_query(_cwd: Path, _q: str, _limit: int, **kwargs: object) -> list[dict[str, object]]:
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

    def fake_query(_cwd: Path, _q: str, _limit: int, **kwargs: object) -> list[dict[str, object]]:
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


def test_from_qmd_drops_phantom_paths(indexed_wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hits whose target file is absent in the wiki tree are dropped, not minted.

    Repro of the orchestrator ``WikiPageNotFound`` crash: qmd returns
    ``opencode/config.md`` (a library path, not under ``wiki.wiki_dir``)
    and ``_from_qmd`` mints ``page-2da7bf8c551d`` for a file that does
    not exist. ``wiki_read`` then raises on the phantom id. Pin the
    fix: phantom paths produce no WikiEvidence, never a phantom id.
    """
    from lies import qmd

    def fake_query(_cwd: Path, _q: str, _limit: int, **kwargs: object) -> list[dict[str, object]]:
        # The path is well-formed (passes validate_page_path) but the
        # file does NOT exist under wiki.wiki_dir — the exact shape
        # the 2026-09-22 transcript reproduced.
        return [{"path": "opencode/config.md", "score": 0.9}]

    monkeypatch.setattr(qmd.cli, "qmd_query", fake_query)
    result = search_wiki(indexed_wiki, "opencode linux settings")
    assert result.pages == [], (
        "phantom hit leaked a WikiEvidence; downstream wiki_read would raise "
        "WikiPageNotFound on the minted page-<hash> id"
    )
    # Phantom dropped at the gate: qmd returned data but zero materialized
    # evidence. ``fallback_reason="qmd_no_results"`` carries that signal
    # so the no_coverage contract fires correctly downstream.
    assert result.fallback_used is True
    assert result.fallback_reason == "qmd_no_results"


def test_search_wiki_threads_collection_filter_to_qmd(
    indexed_wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """search_wiki forwards the ``collection_filter`` kwarg to qmd_search.

    The tag-filtered query path (orchestrator ``run_query`` →
    librarian ``wiki_search`` → ``memory_service.search`` →
    ``search_wiki``) chains ``collection_filter`` down to qmd so the
    post-filter in ``qmd_query`` runs at qmd-time. Without the
    forward, the filter never reaches qmd and the same phantom-path
    bug recurs.
    """
    from lies import qmd

    captured: dict[str, object] = {}

    def fake_query(_cwd: Path, _q: str, _limit: int, **kwargs: object) -> list[dict[str, object]]:
        captured["collection_filter"] = kwargs.get("collection_filter")
        return []

    monkeypatch.setattr(qmd.cli, "qmd_query", fake_query)
    search_wiki(indexed_wiki, "q", collection_filter={"opencode"})
    assert captured["collection_filter"] == {"opencode"}, (
        f"collection_filter did not reach qmd_search: got {captured.get('collection_filter')!r}"
    )


def test_from_qmd_emits_evidence_only_for_existing_files(
    indexed_wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hit path that exists on disk produces WikiEvidence; a phantom does not."""
    from lies import qmd

    def fake_query(_cwd: Path, _q: str, _limit: int, **kwargs: object) -> list[dict[str, object]]:
        # One real path (fixture wrote it) and one phantom.
        return [
            {"path": "concepts/mvc.md", "score": 0.9},
            {"path": "opencode/config.md", "score": 0.8},
        ]

    monkeypatch.setattr(qmd.cli, "qmd_query", fake_query)
    result = search_wiki(indexed_wiki, "q")
    paths = [p.path for p in result.pages]
    assert paths == ["concepts/mvc.md"], (
        f"expected only the real path; got {paths!r} — phantom leaked through"
    )
