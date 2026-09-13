"""End-to-end: wiki with library content + wiki content surfaces both
with source tags in the answer."""

from pathlib import Path

import pytest

from lies import xdg
from lies.constants import LIES_DATA_SUBDIR
from lies.library.paths import Library
from lies.query.synthesizer import FALLBACK_REASON_WIKI_ONLY
from lies.wiki.wiki import Wiki


@pytest.fixture
def mixed_wiki(tmp_path: Path) -> Wiki:
    """A wiki with both library mirror content and wiki-authored content.

    Library mirror: ``claude_platform/skills.md``
    Wiki: ``wiki/concepts/local.md``
    """
    Library.open.cache_clear()

    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    wiki = Wiki(
        name="mixed",
        data_root=root,
        config_root=xdg.config_home() / LIES_DATA_SUBDIR / "mixed",
        cache_root=xdg.cache_home() / LIES_DATA_SUBDIR / "mixed",
        state_root=xdg.state_home() / LIES_DATA_SUBDIR / "mixed",
        runtime_root=xdg.runtime_dir_for("mixed"),
    )

    lib = Library.open()
    mirror_dir = lib.collections_root / "claude_platform"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "skills.md").write_text(
        "---\ntitle: Skills from library\n---\nLibrary content about skills.\n",
        encoding="utf-8",
    )

    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "concepts" / "local.md").write_text(
        "---\ntitle: Local concept\n---\nWiki-authored content.\n",
        encoding="utf-8",
    )

    return wiki


def test_query_returns_library_and_wiki_pages(mixed_wiki: Wiki) -> None:
    """A single retrieve_pages call surfaces both library and wiki
    pages with their respective source tags."""
    from lies.query.synthesizer import retrieve_pages

    Library.open.cache_clear()

    # Patch qmd_query directly so the wrapper sees the same shape
    # real qmd produces but without a subprocess. The first call
    # (library, no wiki_<name> filter) returns the library hit; the
    # second call (wiki-rooted filter) returns the wiki hit.
    call_count = {"n": 0}

    def fake_qmd_query(cwd, q, limit, *, collection_filter=None, **_kw):
        call_count["n"] += 1
        if collection_filter and any(c.startswith("wiki_") for c in collection_filter):
            return [{"path": "concepts/local.md", "score": 0.9}]
        return [{"path": "claude_platform/skills.md", "score": 0.8}]

    pages, reason = retrieve_pages("skills", mixed_wiki, qmd_search=fake_qmd_query)

    assert call_count["n"] == 2
    sources = sorted(p.source for p in pages)
    assert sources == ["library", "wiki"]
    assert reason == ""


def test_query_wiki_only_when_library_empty(mixed_wiki: Wiki) -> None:
    """Library returns 0 hits, wiki returns hits — fallback_reason
    is wiki_only and the body opens with the not-grounded preamble."""
    from lies.query.synthesizer import retrieve_pages

    Library.open.cache_clear()

    def fake_qmd_query(cwd, q, limit, *, collection_filter=None, **_kw):
        if collection_filter and any(c.startswith("wiki_") for c in collection_filter):
            return [{"path": "concepts/local.md", "score": 0.9}]
        return []

    pages, reason = retrieve_pages("anything", mixed_wiki, qmd_search=fake_qmd_query)

    assert reason == FALLBACK_REASON_WIKI_ONLY
    assert len(pages) == 1
    assert pages[0].source == "wiki"
