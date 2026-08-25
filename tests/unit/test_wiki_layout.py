from __future__ import annotations

from pathlib import Path

import pytest

from lies.wiki.layout import WikiLayout, git_init_initial


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
