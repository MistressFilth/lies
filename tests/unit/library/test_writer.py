"""Tests for LibraryWriter (atomic-commit envelope)."""

from pathlib import Path
import sqlite3
import pytest
import subprocess
from lies.library.writer import LibraryWriter
from lies.library.catalog import (
    LibraryCatalogPage,
    open_catalog,
    list_pages,
)
from lies.library.errors import LibraryCatalogLocked
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


def _sample_catalog_update(slug: str) -> LibraryCatalogPage:
    return LibraryCatalogPage(
        slug=slug,
        title="X",
        type="",
        source_pkg="claude",
        section="library",
        updated="2024-01-01",
        hash="",
        derived_from="",
    )


def test_writer_upsert_catalog_raises_locked_when_open_locked(
    lib_with_git: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins existing behavior: lock on open_catalog → LibraryCatalogLocked."""
    import lies.library.writer as writer_mod

    def boom_open_catalog(_library):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(writer_mod, "open_catalog", boom_open_catalog)
    writer = LibraryWriter(lib_with_git)

    with pytest.raises(LibraryCatalogLocked):
        writer._upsert_catalog([_sample_catalog_update("claude/x")])


def test_writer_upsert_catalog_raises_locked_when_commit_locked(
    lib_with_git: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins new behavior: lock during conn.commit() → LibraryCatalogLocked."""
    import lies.library.writer as writer_mod

    real_conn = open_catalog(lib_with_git)

    class _StubConn:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def executemany(self, sql, params):  # type: ignore[no-untyped-def]
            return self._real.executemany(sql, params)

        def commit(self) -> None:
            raise sqlite3.OperationalError("database is locked")

        def close(self) -> None:
            self._real.close()

    def stub_open_catalog(_library):  # type: ignore[no-untyped-def]
        return _StubConn(real_conn)

    monkeypatch.setattr(writer_mod, "open_catalog", stub_open_catalog)
    writer = LibraryWriter(lib_with_git)

    with pytest.raises(LibraryCatalogLocked):
        writer._upsert_catalog([_sample_catalog_update("claude/x")])
    # The stub's close() ran (via finally), but real_conn was already
    # closed by the stub. Verify nothing leaked by re-opening cleanly.
    conn = open_catalog(lib_with_git)
    conn.close()
