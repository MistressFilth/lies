from pathlib import Path
import subprocess
import pytest
from lies.library.ingest import FetchItem, run_batch_ingest
from lies.library.paths import Library


class _StaticFetcher:
    def __init__(self, items: list[FetchItem]) -> None:
        self._items = items

    def fetch_sources(self, source):  # type: ignore[no-untyped-def]
        yield from self._items


@pytest.fixture
def lib(tmp_path: Path, monkeypatch) -> Library:
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".lies").mkdir(parents=True, exist_ok=True)
    # ``.gitkeep`` placeholder: git refuses an empty initial commit, and
    # ``git commit -m "init"`` below fails without at least one staged
    # file. Mirror the pattern from ``tests/unit/library/test_ingest.py``.
    (lib.git_root / ".gitkeep").write_text("")
    subprocess.run(
        ["git", "init", "-b", "main", str(lib.git_root)], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(lib.git_root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(lib.git_root), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(lib.git_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "init"], check=True, capture_output=True
    )
    return lib


def test_ingest_determinism_byte_identical_across_runs(lib: Library) -> None:
    # Bodies must clear the thin-content filter (5+ non-blank lines, see
    # ``filter.py:_MIN_CONTENT_LINES``) so the mirrors actually land on
    # disk instead of being quarantined.
    items = [
        FetchItem(
            path=Path("/src/a.md"),
            url=None,
            body=(
                "# A\n\n"
                "body a line one\n"
                "body a line two\n"
                "body a line three\n"
                "body a line four\n"
                "body a line five\n"
            ),
            source_hash="abc",
            fetched_via="github",
        ),
        FetchItem(
            path=Path("/src/b.md"),
            url=None,
            body=(
                "# B\n\n"
                "body b line one\n"
                "body b line two\n"
                "body b line three\n"
                "body b line four\n"
                "body b line five\n"
            ),
            source_hash="def",
            fetched_via="github",
        ),
    ]
    fetcher = _StaticFetcher(items)

    run_batch_ingest(lib, "claude", source_dir=Path("/src"), fetcher=fetcher)
    first_a = (lib.collections_root / "claude" / "a.md").read_bytes()
    first_b = (lib.collections_root / "claude" / "b.md").read_bytes()

    # Reset library (delete mirrors but keep git history).
    for p in (lib.collections_root / "claude").glob("*.md"):
        p.unlink()
    subprocess.run(["git", "-C", str(lib.git_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "reset"], check=True, capture_output=True
    )

    fetcher2 = _StaticFetcher(items)
    run_batch_ingest(lib, "claude", source_dir=Path("/src"), fetcher=fetcher2)
    second_a = (lib.collections_root / "claude" / "a.md").read_bytes()
    second_b = (lib.collections_root / "claude" / "b.md").read_bytes()

    assert first_a == second_a
    assert first_b == second_b


def test_ingest_determinism_operator_flag_break_contract(lib: Library) -> None:
    """Negative control: --exclude-stem change breaks the contract."""
    items = [
        FetchItem(
            path=Path("/src/robots.md"),
            url=None,
            body="body\n" * 20,
            source_hash="aaa",
            fetched_via="github",
        ),
    ]
    fetcher = _StaticFetcher(items)
    run_batch_ingest(lib, "claude", source_dir=Path("/src"), fetcher=fetcher)
    # Side-effect: verify robots.md landed on disk before the exclude-stem
    # reset (the assertion further down checks only the post-exclude state).
    (lib.collections_root / "claude" / "robots.md").read_bytes()

    (lib.collections_root / "claude" / "robots.md").unlink()
    subprocess.run(["git", "-C", str(lib.git_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "reset"], check=True, capture_output=True
    )

    fetcher2 = _StaticFetcher(items)
    run_batch_ingest(
        lib, "claude", source_dir=Path("/src"), fetcher=fetcher2, exclude_stems={"robots"}
    )
    second_dir = lib.collections_root / "claude"
    # robots.md should NOT exist after the second run.
    assert not (second_dir / "robots.md").exists()
