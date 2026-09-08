"""Tests for the 5-step deterministic ingest pipeline orchestrator."""

from collections.abc import Iterator
from pathlib import Path
import subprocess
import pytest
from lies.library.errors import LibraryFetchUnreachable
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


class _RaisingFetcher(Fetcher):
    """Test double: yields items, then raises once to simulate dispatch failure.

    Mirrors ``ScraperFetcher``'s shape: the exception is raised mid-iteration
    (after the first yield), the way ``_normalize_body`` raises when
    ``format_dispatch.dispatch`` rejects an unknown ``source_format``.
    """

    def __init__(self, items: list[FetchItem], exc: BaseException) -> None:
        self._items = items
        self._exc = exc

    def fetch_sources(self, source: Path | str) -> Iterator[FetchItem]:
        for item in self._items:
            yield item
        raise self._exc


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


def test_run_source_ingest_force_overwrite_counts_as_updated(lib_with_git: Library) -> None:
    item = FetchItem(
        path=Path("/src/x.md"),
        url=None,
        body="body\n" * 10,
        source_hash="abc123",
        fetched_via="github",
    )
    fetcher = _StaticFetcher([item])

    first = run_source_ingest(lib_with_git, "claude", source="x", fetcher=fetcher)
    assert first.created == 1

    result = run_source_ingest(lib_with_git, "claude", source="x", fetcher=fetcher, force=True)

    assert result.updated == 1
    assert result.created == 0


def test_run_source_ingest_mirror_collision_reports_existing_and_new_hashes(
    lib_with_git: Library,
) -> None:
    body = "body\n" * 10
    first = FetchItem(
        path=Path("/src/x.md"),
        url=None,
        body=body,
        source_hash="a1b2c3d4existing",
        fetched_via="github",
    )
    incoming = FetchItem(
        path=Path("/src/x.md"),
        url=None,
        body=body,
        source_hash="deadbeefnew",
        fetched_via="github",
    )

    run_source_ingest(lib_with_git, "claude", source="x", fetcher=_StaticFetcher([first]))
    result = run_source_ingest(
        lib_with_git, "claude", source="x", fetcher=_StaticFetcher([incoming])
    )

    reason = next(reason for _, reason in result.quarantine_records)
    assert "existing-a1b2c3d4" in reason
    assert "new-deadbeef" in reason
    # Pin the fail-loud contract: genuine hash mismatch must bump errors so
    # `errors > 0 → exit 1` fires through the CLI (Fix #1). Without this
    # assertion, a regression that quarantines mismatches without bumping
    # errors would silently slip through.
    assert result.errors == 1


def test_run_source_ingest_same_hash_mirror_collision_is_idempotent_skip(
    lib_with_git: Library,
) -> None:
    """Re-ingesting an unchanged source is a skip, not an error.

    Regression for Task 11 fix #2: the collision branch in
    ``_process_item`` was always bumping ``result.errors`` whenever a
    target mirror existed, even when the incoming ``source_hash``
    matched the existing mirror's frontmatter ``source_hash``. With
    ``errors > 0 → exit 1`` wired through the CLI (Fix #1), idempotent
    re-runs of unchanged sources started exiting 1 — a silent regression
    from the previous exit-0 behavior. The fix reads the existing
    mirror's frontmatter and treats a matching hash as
    ``mirror-collision:up_to_date``: skip, no error, no quarantine.
    """
    body = "body\n" * 10
    item = FetchItem(
        path=Path("/src/x.md"),
        url=None,
        body=body,
        source_hash="samehashdeadbeef",
        fetched_via="github",
    )

    first = run_source_ingest(lib_with_git, "claude", source="x", fetcher=_StaticFetcher([item]))
    assert first.created == 1
    assert first.errors == 0

    # Second sync with the same hash — mirror collision must NOT bump
    # errors, NOT add a quarantine record, and must record a skip with
    # the ``mirror-collision:up_to_date`` reason.
    result = run_source_ingest(lib_with_git, "claude", source="x", fetcher=_StaticFetcher([item]))

    assert result.errors == 0
    assert result.quarantine_records == []
    assert result.skipped >= 1
    assert result.skip_reasons.get("mirror-collision", 0) >= 1
    # The on-disk mirror was not touched by the second sync (it
    # short-circuited before reaching ``write_mirror``).
    assert result.created == 0
    assert result.updated == 0
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


def test_run_source_ingest_dispatch_failure_quarantines_and_continues(
    lib_with_git: Library,
) -> None:
    """A mid-batch ``UnknownFormatError`` quarantines the bad doc, keeps the good one.

    Regression for the fetcher-fix follow-up: ``ScraperFetcher`` raises
    ``UnknownFormatError`` mid-yield when ``format_dispatch.dispatch`` rejects
    a ``source_format``. Without per-doc quarantine at the ingest boundary,
    that exception aborts the whole batch and the good items that yielded
    before the failure are lost.

    The pipeline must:
    - return normally (not raise);
    - record the bad doc in ``quarantine_records`` with a
      ``fetch-unreachable``-prefixed reason;
    - increment ``errors`` for the quarantined doc;
    - still process the items that yielded before the failure.
    """
    from lies.etl.normalize.format_dispatch import UnknownFormatError

    valid = FetchItem(
        path=Path("/src/valid.md"),
        url=None,
        body="body\n" * 10,
        source_hash="aaa",
        fetched_via="github",
    )
    fetcher = _RaisingFetcher(
        [valid],
        UnknownFormatError("unknown source format: 'liquid'"),
    )
    result = run_source_ingest(lib_with_git, "claude", source="x", fetcher=fetcher)

    # The good item made it through; the dispatch failure was quarantined
    # without aborting the run.
    assert result.created == 1
    assert result.errors == 1
    assert len(result.quarantine_records) == 1
    poison_path, reason = result.quarantine_records[0]
    assert "fetch-unreachable" in reason
    assert "UnknownFormatError" in reason
    # The poison path encodes the source + a sentinel for the bad doc.
    assert "x" in poison_path

    coll = lib_with_git.collection("claude")
    assert (coll.dir / "valid.md").exists()


def test_run_source_ingest_dispatch_failure_when_zero_items_yielded(
    lib_with_git: Library,
) -> None:
    """When every doc fails dispatch, the run continues with a quarantine record.

    Previously the whole batch would abort and surface the dispatch
    exception to the operator. With per-doc quarantine, an empty items
    list paired with a non-empty ``quarantine_records`` is a soft failure:
    the run returns normally, ``errors`` reflects the bad doc, and
    ``LibraryFetchUnreachable`` is NOT raised (the run was not a no-op —
    the fetcher reported the failure explicitly).
    """
    from lies.etl.normalize.format_dispatch import UnknownFormatError

    fetcher = _RaisingFetcher(
        [],
        UnknownFormatError("unknown source format: 'liquid'"),
    )
    result = run_source_ingest(lib_with_git, "claude", source="x", fetcher=fetcher)

    assert result.errors == 1
    assert result.created == 0
    assert len(result.quarantine_records) == 1
    _, reason = result.quarantine_records[0]
    assert "fetch-unreachable" in reason
    assert "UnknownFormatError" in reason


def test_run_source_ingest_zero_items_no_quarantine_still_raises(
    lib_with_git: Library,
) -> None:
    """A truly-empty fetcher (no dispatch in play) still raises.

    Preserves the existing ``LibraryFetchUnreachable`` semantic: when the
    fetcher yields zero items AND no per-doc quarantine records exist, the
    run is a no-op and aborts so the operator notices.
    """
    fetcher = _StaticFetcher([])
    with pytest.raises(LibraryFetchUnreachable):
        run_source_ingest(lib_with_git, "claude", source="x", fetcher=fetcher)


def test_run_batch_ingest_dispatch_failure_quarantines_and_continues(
    lib_with_git: Library,
) -> None:
    """``run_batch_ingest`` applies the same per-doc quarantine contract.

    ``_iter_fetch_items`` is shared by both call sites; this guards the
    second call site explicitly.
    """
    from lies.etl.normalize.format_dispatch import UnknownFormatError

    valid_a = FetchItem(
        path=Path("/src/a.md"),
        url=None,
        body="body a\n" * 10,
        source_hash="aaa",
        fetched_via="github",
    )
    valid_b = FetchItem(
        path=Path("/src/b.md"),
        url=None,
        body="body b\n" * 10,
        source_hash="bbb",
        fetched_via="github",
    )
    fetcher = _RaisingFetcher(
        [valid_a, valid_b],
        UnknownFormatError("unknown source format: 'liquid'"),
    )
    result = run_batch_ingest(lib_with_git, "claude", Path("/src"), fetcher=fetcher)

    assert result.created == 2
    assert result.errors == 1
    assert len(result.quarantine_records) == 1
    _, reason = result.quarantine_records[0]
    assert "fetch-unreachable" in reason


def test_run_source_ingest_generic_dispatch_exception_is_quarantined(
    lib_with_git: Library,
) -> None:
    """Any per-doc dispatch exception is quarantined, not just ``UnknownFormatError``.

    The spec says "any per-doc exception from ``format_dispatch.dispatch``"
    — the catch-all in ``_iter_fetch_items`` must cover more than the
    one known class so a future format-dispatch error doesn't silently
    abort the batch.
    """
    valid = FetchItem(
        path=Path("/src/valid.md"),
        url=None,
        body="body\n" * 10,
        source_hash="aaa",
        fetched_via="github",
    )
    fetcher = _RaisingFetcher([valid], RuntimeError("pandoc daemon died"))
    result = run_source_ingest(lib_with_git, "claude", source="x", fetcher=fetcher)

    assert result.created == 1
    assert result.errors == 1
    _, reason = result.quarantine_records[0]
    assert "fetch-unreachable" in reason
    assert "RuntimeError" in reason
