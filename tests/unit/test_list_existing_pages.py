"""Unit tests for ``Orchestrator._list_existing_pages``.

The walker returns ``(wiki-relative path, summary)`` pairs for every page
under ``wiki/<collection>/`` (or ``data_root / wiki / <collection>``),
skipping the deterministic shell pages (``index.md``, ``log.md``) and
anything under ``.lies/`` or ``.git/``. Summary extractor: frontmatter
``summary:`` first; otherwise first H1 plus first non-empty body line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.orchestrator import Orchestrator
from tests.conftest import make_wiki, models_for_tests


@pytest.fixture
def wiki(tmp_path: Path):
    """A wiki rooted at ``tmp_path`` with ``wiki/`` and ``raw/`` prepared."""
    data_root = tmp_path
    (data_root / "wiki").mkdir(parents=True, exist_ok=True)
    (data_root / "raw").mkdir(parents=True, exist_ok=True)
    return make_wiki(name="list-pages", data_root=data_root)


def test_list_existing_pages_returns_paths_and_summaries(wiki) -> None:
    """Two pages in collection ``foo`` — one with frontmatter summary,
    one without. Both should appear; the frontmatter summary wins for
    beta; alpha gets the deterministic H1 + first body line fallback.
    """
    collection_dir = wiki.data_root / "wiki" / "foo" / "concepts"
    collection_dir.mkdir(parents=True)
    (collection_dir / "alpha.md").write_text(
        "---\ntitle: Alpha\n---\n# Alpha\n\nIntroduces the concept.\n",
        encoding="utf-8",
    )
    (collection_dir / "beta.md").write_text(
        "---\ntitle: Beta\nsummary: A short summary.\n---\n",
        encoding="utf-8",
    )
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))

    pages = orch._list_existing_pages("foo")
    by_path = {p: s for p, s in pages}

    assert "wiki/foo/concepts/alpha.md" in by_path
    assert "Introduces the concept" in by_path["wiki/foo/concepts/alpha.md"]
    assert by_path["wiki/foo/concepts/beta.md"] == "A short summary."


def test_list_existing_pages_excludes_index_log_and_lies(wiki) -> None:
    """``index.md``, ``log.md``, and anything under ``.lies/`` are filtered out."""
    foo_dir = wiki.data_root / "wiki" / "foo"
    foo_dir.mkdir(parents=True)
    (foo_dir / "index.md").write_text("# Index\n", encoding="utf-8")
    (foo_dir / "log.md").write_text("# Log\n", encoding="utf-8")
    lies_dir = foo_dir / ".lies"
    lies_dir.mkdir(parents=True)
    (lies_dir / "memory_plans.jsonl").write_text("ignore", encoding="utf-8")
    # A real page in the same collection that SHOULD be listed.
    (foo_dir / "concepts").mkdir(parents=True)
    (foo_dir / "concepts" / "real.md").write_text("# Real\n\nBody.\n", encoding="utf-8")

    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))
    pages = orch._list_existing_pages("foo")
    paths = {p for p, _ in pages}

    assert "wiki/foo/index.md" not in paths
    assert "wiki/foo/log.md" not in paths
    assert not any(".lies" in p for p in paths)
    assert "wiki/foo/concepts/real.md" in paths


def test_list_existing_pages_empty_collection_returns_empty_list(wiki) -> None:
    """A collection that doesn't exist on disk yields ``[]`` (not an error)."""
    orch = Orchestrator(wiki=wiki, models=models_for_tests("test"))
    assert orch._list_existing_pages("nonexistent") == []
