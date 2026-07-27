from __future__ import annotations

from pathlib import Path

import pytest

from lies.wiki.layout import WikiLayout


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".lies").mkdir()
    return tmp_path


def test_layout_resolves_paths(wiki_root: Path) -> None:
    layout = WikiLayout(wiki_root)
    assert layout.root == wiki_root
    assert layout.raw_dir == wiki_root / "raw"
    assert layout.wiki_dir == wiki_root / "wiki"
    assert layout.schema_path == wiki_root / ".lies" / "schema.md"
    assert layout.index_path == wiki_root / "wiki" / "index.md"
    assert layout.log_path == wiki_root / "wiki" / "log.md"
    assert layout.overview_path == wiki_root / "wiki" / "overview.md"


def test_layout_is_repo(wiki_root: Path) -> None:
    import subprocess

    subprocess.run(
        ["git", "init", "--initial-branch=main", str(wiki_root)],
        check=True,
        capture_output=True,
    )
    layout = WikiLayout(wiki_root)
    assert layout.is_git_repo() is True


def test_layout_not_repo(tmp_path: Path) -> None:
    # tmp_path is a fresh empty dir; even if a parent repo exists, this
    # dir is not itself a work tree.
    layout = WikiLayout(tmp_path)
    assert layout.is_git_repo() is False


def test_layout_not_repo_when_parent_is_repo(tmp_path: Path) -> None:
    # Init a repo at a parent of tmp_path; tmp_path itself is not a work tree.
    import subprocess

    parent = tmp_path.parent
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(parent)],
        check=True,
        capture_output=True,
    )
    layout = WikiLayout(tmp_path)
    assert layout.is_git_repo() is False


def test_layout_page_paths(wiki_root: Path) -> None:
    layout = WikiLayout(wiki_root)
    page = layout.page_path("entities", "alice")
    assert page == wiki_root / "wiki" / "entities" / "alice.md"
