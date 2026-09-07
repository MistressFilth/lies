from __future__ import annotations

from pathlib import Path

import pytest

from lies.wiki.layout import WikiLayout, _gitignore_lines, git_init_initial


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki").mkdir()
    return tmp_path


def test_layout_resolves_paths(wiki_root: Path) -> None:
    layout = WikiLayout(wiki_root)
    assert layout.root == wiki_root
    assert layout.raw_dir == wiki_root / "raw"
    assert layout.wiki_dir == wiki_root / "wiki"


def test_init_creates_raw_and_wiki_dirs(tmp_path: Path) -> None:
    WikiLayout(tmp_path).init()
    assert (tmp_path / "raw").is_dir()
    assert (tmp_path / "wiki").is_dir()


def test_init_is_idempotent(tmp_path: Path) -> None:
    WikiLayout(tmp_path).init()
    # A second call must not raise; init() uses exist_ok=True.
    WikiLayout(tmp_path).init()
    assert (tmp_path / "raw").is_dir()
    assert (tmp_path / "wiki").is_dir()


def test_git_init_initial_writes_gitignore_excluding_lies(tmp_path: Path) -> None:
    """``git_init_initial`` writes ``<wiki>/.gitignore`` covering ``.lies/``.

    The sidecar at ``<wiki>/.lies/memory_plans.jsonl`` is untracked on every
    fresh wiki. ``WikiMemoryService.apply_plan`` snapshots the working tree
    via ``git stash push --include-untracked``; without this ignore the
    untracked sidecar is stashed and dropped on success, silently losing
    prior sidecar lines.
    """
    WikiLayout(tmp_path).init()
    git_init_initial(tmp_path)
    gitignore = tmp_path / ".gitignore"
    assert gitignore.is_file()
    assert ".lies/\n" in gitignore.read_text(encoding="utf-8")


def test_gitignore_lines_includes_catalog_pattern() -> None:
    """The catalog entry is anchored under ``wiki/``; root-anchored entries are gone.

    The catalog lives at ``<wiki>/wiki/.lies/catalog.db`` (not at the
    wiki root), so a single ``wiki/.lies/catalog.db*`` pattern
    subsumes the database, WAL, and SHM siblings.
    """
    lines = _gitignore_lines()
    assert any("wiki/.lies/catalog.db*" in line for line in lines)
    assert not any(line.startswith(".lies/catalog.db") for line in lines), (
        "root-anchored entries should be removed"
    )


def test_git_init_initial_does_not_clobber_existing_gitignore(tmp_path: Path) -> None:
    """A pre-existing ``.gitignore`` is preserved (M4)."""
    WikiLayout(tmp_path).init()
    custom = "# my operator-curated ignores\n*.bak\n"
    (tmp_path / ".gitignore").write_text(custom, encoding="utf-8")
    git_init_initial(tmp_path)
    gitignore = tmp_path / ".gitignore"
    assert gitignore.read_text(encoding="utf-8") == custom
