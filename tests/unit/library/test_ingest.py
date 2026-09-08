"""Tests for the 5-step deterministic ingest pipeline orchestrator."""

from collections.abc import Iterator
from pathlib import Path
import subprocess
import pytest
from lies.library.ingest import (
    run_source_ingest,
    run_batch_ingest,
    FetchItem,
    Fetcher,
)
from lies.library.paths import Library


class _StaticFetcher(Fetcher):
    """Test double: emits one or more pre-defined FetchItems."""

    def __init__(self, items: list[FetchItem]) -> None:
        self._items = items

    def fetch_sources(self, source: Path | str) -> Iterator[FetchItem]:
        yield from self._items


@pytest.fixture
def lib(tmp_path: Path, monkeypatch) -> Library:
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    yield Library.open()


@pytest.fixture
def lib_with_git(lib: Library) -> Library:
    """Initialise a git repo at library.git_root with a baseline commit.

    Mirror of ``test_writer.lib_with_git``: ``.gitkeep`` placeholder inside
    the empty ``.lies/`` so ``git add .`` has something to stage.
    """
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
        ["git", "-C", str(lib.git_root), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return lib


def test_run_source_ingest_basic(lib_with_git: Library) -> None:
    fetcher = _StaticFetcher(
        [
            FetchItem(
                path=Path("/src/x.md"),
                url=None,
                # 7 non-blank lines so should_skip_content's thin-content gate
                # (threshold 5, ``<= _MIN_CONTENT_LINES``, see Task 5's
                # filter.py) doesn't quarantine it.
                body=(
                    "# Title\n\n"
                    "body line one\n"
                    "body line two\n"
                    "body line three\n"
                    "body line four\n"
                    "body line five\n"
                ),
                source_hash="abc123",
                fetched_via="github",
            ),
        ]
    )
    result = run_source_ingest(
        lib_with_git, "claude", source="https://example.com/x", fetcher=fetcher
    )
    assert result.created == 1
    assert result.errors == 0
    coll = lib_with_git.collection("claude")
    assert (coll.dir / "x.md").exists()


def test_run_source_ingest_filter_skip(lib_with_git: Library) -> None:
    fetcher = _StaticFetcher(
        [
            FetchItem(
                path=Path("/src/LICENSE"),
                url=None,
                body="License text\n" + "filler\n" * 10,
                source_hash="def456",
                fetched_via="github",
            ),
        ]
    )
    result = run_source_ingest(lib_with_git, "claude", source="x", fetcher=fetcher)
    assert result.skipped >= 1


def test_run_source_ingest_thin_content_quarantine(lib_with_git: Library) -> None:
    fetcher = _StaticFetcher(
        [
            FetchItem(
                path=Path("/src/x.md"),
                url=None,
                body="tiny\n",
                source_hash="aaa",
                fetched_via="github",
            ),
        ]
    )
    result = run_source_ingest(lib_with_git, "claude", source="x", fetcher=fetcher)
    assert result.errors >= 1
    assert any(r[0].endswith("x.md") for r in result.quarantine_records)


def test_run_source_ingest_dry_run_no_write(lib_with_git: Library) -> None:
    fetcher = _StaticFetcher(
        [
            FetchItem(
                path=Path("/src/x.md"),
                url=None,
                body="body\n" * 10,
                source_hash="bbb",
                fetched_via="github",
            ),
        ]
    )
    result = run_source_ingest(lib_with_git, "claude", source="x", fetcher=fetcher, dry_run=True)
    coll = lib_with_git.collection("claude")
    assert not (coll.dir / "x.md").exists()
    assert result.created == 0


def test_run_batch_ingest_walks_dir(lib_with_git: Library, tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("body a\n" * 10)
    (tmp_path / "b.md").write_text("body b\n" * 10)
    (tmp_path / "LICENSE").write_text("License\n" * 10)
    # Walk every regular file (not just ``*.md``) so LICENSE reaches the
    # fetcher and exercises the filename-skip gate.
    fetcher = _StaticFetcher(
        [
            FetchItem(
                path=p,
                url=None,
                body=p.read_text(),
                source_hash="hash" + p.name,
                fetched_via="github",
            )
            for p in sorted(tmp_path.iterdir())
            if p.is_file()
        ]
    )
    result = run_batch_ingest(lib_with_git, "claude", tmp_path, fetcher=fetcher)
    assert result.created == 2
    assert result.skipped >= 1  # LICENSE filtered by filename gate
