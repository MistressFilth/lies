"""Tests for LibraryWriter (atomic-commit envelope)."""

from pathlib import Path
import pytest
import subprocess
from lies.library.writer import LibraryWriter
from lies.library.catalog import (
    LibraryCatalogPage,
    open_catalog,
    list_pages,
)
from lies.library.paths import Library


@pytest.fixture
def lib(tmp_path: Path, monkeypatch) -> Library:
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    yield Library.open()


@pytest.fixture
def lib_with_git(lib: Library) -> Library:
    """Initialise a git repo at library.git_root with a baseline commit."""
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".lies").mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("")
    subprocess.run(
        ["git", "init", "-b", "main", str(lib.git_root)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.email", "test@test"], check=True
    )
    subprocess.run(["git", "-C", str(lib.git_root), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(lib.git_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "init"], check=True, capture_output=True
    )
    return lib


def test_writer_commit_records_sha(lib_with_git: Library, tmp_path: Path) -> None:
    file = lib_with_git.collections_root / "claude" / "getting-started.md"
    file.parent.mkdir(parents=True)
    file.write_text("body\n")
    rel = file.relative_to(lib_with_git.git_root)
    writer = LibraryWriter(lib_with_git)
    sha = writer.commit([rel], message="ingest: claude +1")
    assert sha is not None
    assert len(sha) == 40


def test_writer_commit_no_op_returns_none(lib_with_git: Library) -> None:
    writer = LibraryWriter(lib_with_git)
    result = writer.commit([], message="empty")
    assert result is None


def test_writer_commit_catalog_updates(lib_with_git: Library) -> None:
    file = lib_with_git.collections_root / "claude" / "x.md"
    file.parent.mkdir(parents=True)
    file.write_text("body\n")
    rel = file.relative_to(lib_with_git.git_root)
    writer = LibraryWriter(lib_with_git)
    sha = writer.commit(
        [rel],
        message="ingest: claude +1",
        catalog_updates=[
            LibraryCatalogPage(
                slug="claude/x",
                title="X",
                type="",
                source_pkg="claude",
                section="library",
                updated="2024-01-01",
                hash="",
                derived_from="",
            ),
        ],
    )
    assert sha is not None
    conn = open_catalog(lib_with_git)
    try:
        slugs = [p.slug for p in list_pages(conn, section="library")]
    finally:
        conn.close()
    assert slugs == ["claude/x"]
