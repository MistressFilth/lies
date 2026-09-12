"""Tests for the 5-step deterministic ingest pipeline orchestrator."""

from collections.abc import Iterator
from pathlib import Path
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


@pytest.fixture(autouse=True)
def _mock_atomic_commit_and_qmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the two external-service boundaries in the ingest pipeline.

    ``LibraryWriter.commit`` shells out to ``atomic_commit`` (5 git
    subprocess calls per write) and then to the qmd CLI helpers
    (each of which is another ``subprocess.run``). Both are real
    subprocess services; this fixture stubs them so the unit suite
    exercises the ingest logic without paying fork+exec cost or
    depending on a git/qmd install.
    """

    def fake_atomic_commit(repo: Path, message: str, files=None):  # type: ignore[no-untyped-def]
        return "deadbeef" * 5  # 40-char SHA; matches ``atomic_commit``'s contract

    def fake_qmd(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("lies.library.writer.atomic_commit", fake_atomic_commit)
    monkeypatch.setattr("lies.library.writer.qmd_collection_add_or_update", fake_qmd)
    monkeypatch.setattr("lies.library.writer.qmd_update", fake_qmd)
    monkeypatch.setattr("lies.library.writer.qmd_embed", fake_qmd)


@pytest.fixture
def lib_with_git(lib: Library) -> Library:
    """Provide a ``Library`` with the on-disk shape the pipeline expects.

    No real ``git init`` runs — ``atomic_commit`` is stubbed by the
    autouse ``_mock_atomic_commit_and_qmd`` fixture, so the write path
    never shells out. The ``.lies/`` dir and ``.gitkeep`` placeholder
    still get created so any non-mocked code that introspects the repo
    shape sees something sensible.
    """
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".lies").mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("")
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


def test_run_source_ingest_frontmatter_unparseable_quarantines(
    lib_with_git: Library,
) -> None:
    """I16: a malformed frontmatter on the existing mirror surfaces a typed reason.

    Previously the ``try: frontmatter.loads(...) except Exception: pass``
    swallowed the parse error and silently downgraded the existing
    mirror to ``hash=""``. That made the idempotency check always
    miss and the run always quarantined with ``mirror-collision`` —
    the operator never learned the mirror is malformed. The new
    contract: surface ``frontmatter-unparseable:<slug>:<ExceptionName>``
    in ``quarantine_records`` so the operator can see the real failure.
    """
    body = "body\n" * 10
    coll = lib_with_git.collection("claude")
    target = coll.dir / "x.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Malformed YAML frontmatter: unbalanced quote.
    target.write_text('---\ntitle: "Unterminated\n---\n# body\n', encoding="utf-8")

    item = FetchItem(
        path=Path("/src/x.md"),
        url=None,
        body=body,
        source_hash="abc",
        fetched_via="github",
    )
    result = run_source_ingest(lib_with_git, "claude", source="x", fetcher=_StaticFetcher([item]))
    # The unparseable mirror produces an explicit quarantine reason
    # rather than a misleading mirror-collision.
    assert any("frontmatter-unparseable" in reason for _, reason in result.quarantine_records), (
        f"expected frontmatter-unparseable reason; got {result.quarantine_records!r}"
    )


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


def test_run_source_ingest_quarantine_writes_reason_sidecar(lib_with_git: Library) -> None:
    """C5 anti-tautology: quarantine writes BOTH body AND ``.reason`` sidecar.

    Spec §Error handling row 3b mandates
    ``<poison>/<collection>/<slug>.md + <slug>.md.reason``. The
    sidecar carries the typed reason verbatim so the operator can
    read it from the filesystem without parsing the result API.
    """
    fetcher = _StaticFetcher(
        [
            FetchItem(
                path=Path("/src/x.md"),
                url=None,
                body="tiny\n",  # thin → quarantine
                source_hash="aaa",
                fetched_via="github",
            ),
        ]
    )
    result = run_source_ingest(lib_with_git, "claude", source="x", fetcher=fetcher)
    assert result.errors >= 1
    body_path = lib_with_git.poison_root / "claude" / "x.md"
    assert body_path.exists(), f"quarantine body missing at {body_path}"
    reason_path = lib_with_git.poison_root / "claude" / "x.md.reason"
    assert reason_path.exists(), f"quarantine .reason sidecar missing at {reason_path}"
    expected_reason = result.quarantine_records[0][1]
    assert reason_path.read_text(encoding="utf-8") == expected_reason


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


def test_run_batch_ingest_production_fetcher_walks_directory(
    lib_with_git: Library, tmp_path: Path
) -> None:
    """``ScraperFetcher`` (the production fetcher wired into the CLI) must
    walk a directory and yield one ``FetchItem`` per file.

    Regression pin for the ``--batch`` dispatch defect: the prior
    ``ScraperFetcher.fetch_sources`` called
    ``pick_scraper(source)`` unconditionally, and ``pick_scraper`` had no
    directory branch — so any directory input raised
    ``ScraperUnavailable`` at the dispatch step before ``_process_item``
    ever saw an item.  The existing ``test_run_batch_ingest_walks_dir``
    above masked the defect by using ``_StaticFetcher`` (a test double
    that hand-rolls ``FetchItem``s); this test drives the production
    fetcher end-to-end.

    The fetcher is responsible for: reading each entry, hashing the
    bytes, and yielding a ``FetchItem`` per file with ``url=None`` and
    ``fetched_via="local"``.  Per-doc slug derivation, skip/quarantine
    filters, mirror write, and atomic commit remain ``_process_item``
    and ``_finalize``'s job.
    """
    from lies.library.fetcher import ScraperFetcher

    (tmp_path / "alpha.md").write_text("alpha body\n" * 12)
    (tmp_path / "beta.md").write_text("beta body\n" * 12)
    (tmp_path / "LICENSE").write_text("License\n" * 12)
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "skip-me.md").write_text("nested body\n" * 12)

    fetcher = ScraperFetcher(lib_with_git)
    result = run_batch_ingest(lib_with_git, "claude", tmp_path, fetcher=fetcher)

    # Two mirror files written; LICENSE filtered by filename stem gate;
    # nested file NOT walked (depth-1 only — see dispatch contract).
    assert result.created == 2, (
        f"expected 2 mirrors written, got created={result.created} "
        f"skipped={result.skipped} errors={result.errors}"
    )
    coll_dir = lib_with_git.collections_root / "claude"
    written = {p.name for p in coll_dir.iterdir() if p.is_file()}
    assert written == {"alpha.md", "beta.md"}, written


def test_run_batch_ingest_empty_directory_is_noop(lib_with_git: Library, tmp_path: Path) -> None:
    """Empty ``--batch`` directory is a no-op (spec L400: exit 0, no commit).

    Regression pin for the directory-walk dispatch: before the fix, an
    empty directory raised ``LibraryFetchUnreachable`` because
    ``pick_scraper(source)`` rejected directory inputs entirely; the
    CLI would exit non-zero on ``lies ingest --batch <empty-dir>``.
    """
    from lies.library.fetcher import ScraperFetcher

    (tmp_path / "junk").mkdir()  # Subdirectories are skipped, not yielded.

    fetcher = ScraperFetcher(lib_with_git)
    result = run_batch_ingest(lib_with_git, "claude", tmp_path, fetcher=fetcher)
    assert result.created == 0
    assert result.updated == 0
    assert result.errors == 0
    coll_dir = lib_with_git.collections_root / "claude"
    assert not coll_dir.exists() or not any(coll_dir.iterdir()), (
        f"empty batch must not write any mirrors, found: "
        f"{list(coll_dir.iterdir()) if coll_dir.exists() else 'no coll_dir'}"
    )


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


def test_run_source_ingest_wires_qmd_collection_through_finalize(
    lib_with_git: Library,
) -> None:
    """End-to-end: ``run_source_ingest`` passes ``qmd_collection`` to ``LibraryWriter.commit``.

    Regression for the dormant-hook fix (Task 12 follow-up):
    ``LibraryWriter.commit`` accepts ``qmd_collection: str | None`` to
    fire the post-commit qmd hook against the library path. ``_finalize``
    MUST thread ``collection_name`` through, otherwise the qmd hook is
    dormant in production. This test wires the full ingest pipeline
    (no ``run_*_ingest`` mocking) and asserts the qmd hook actually
    fires with the library target — a regression that drops the kwarg
    in ``_finalize`` leaves the hook off and the assertion catches it.
    """
    from unittest.mock import patch

    fetcher = _StaticFetcher(
        [
            FetchItem(
                path=Path("/src/x.md"),
                url=None,
                body="body\n" * 10,
                source_hash="abc123",
                fetched_via="github",
            ),
        ]
    )

    called: dict[str, object] = {"args": None, "kwargs": None}

    def fake_qmd(*args, **kwargs):  # type: ignore[no-untyped-def]
        called["args"] = args
        called["kwargs"] = kwargs

    with patch("lies.library.writer.qmd_collection_add_or_update", side_effect=fake_qmd):
        with patch("lies.library.writer.qmd_update"):
            with patch("lies.library.writer.qmd_embed"):
                result = run_source_ingest(
                    lib_with_git, "claude", source="https://example.com/x", fetcher=fetcher
                )

    assert result.created == 1
    assert result.errors == 0
    # The qmd hook must have fired against the library collection dir.
    assert called["args"] is not None, "qmd_collection_add_or_update was never called"
    args, kwargs = called["args"], called["kwargs"]
    # Positional: (library.git_root, library.collections_root, qmd_name)
    assert args[0] == lib_with_git.git_root
    assert args[1] == lib_with_git.collections_root
    assert args[2] == "claude"
    # Kwarg: library_target=coll_dir
    assert "library_target" in kwargs
    assert kwargs["library_target"] == lib_with_git.collections_root / "claude"


def test_run_source_ingest_catalog_row_carries_source_hash(
    lib_with_git: Library,
) -> None:
    """Minor 29 anti-tautology: catalog row's ``hash`` column carries source_hash.

    The previous implementation built catalog rows with ``hash=""``,
    discarding the upstream SHA256 of the raw fetch bytes. That made
    catalog-level dedup / qmd change-detection impossible without re-parsing
    the mirror frontmatter. The fix threads ``item.source_hash`` through
    ``BatchIngestResult.mirror_source_hashes`` into the catalog row.
    """
    from lies.library.catalog import open_catalog, list_pages

    fetcher = _StaticFetcher(
        [
            FetchItem(
                path=Path("/src/x.md"),
                url=None,
                body="body\n" * 10,
                source_hash="deadbeef" * 8,  # 64 hex chars — full SHA256
                fetched_via="github",
            ),
        ]
    )
    result = run_source_ingest(lib_with_git, "claude", source="x", fetcher=fetcher)
    assert result.created == 1
    conn = open_catalog(lib_with_git)
    try:
        pages = list_pages(conn, section="library")
    finally:
        conn.close()
    assert len(pages) == 1
    assert pages[0].hash == "deadbeef" * 8, (
        f"catalog row should carry the upstream source_hash; got {pages[0].hash!r}"
    )


def test_run_batch_ingest_catalog_rows_carry_source_hashes(
    lib_with_git: Library,
) -> None:
    """Minor 29 anti-tautology: per-doc source_hash lands in the matching catalog row.

    Pins the parallel-list invariant (``mirror_paths`` and
    ``mirror_source_hashes`` are index-aligned). Off-by-one or alignment
    drift between the two lists would tag a catalog row with the wrong
    upstream hash — a silent data-integrity bug.
    """
    from lies.library.catalog import open_catalog, list_pages

    a = FetchItem(
        path=Path("/src/a.md"),
        url=None,
        body="body a\n" * 10,
        source_hash="a" * 64,
        fetched_via="github",
    )
    b = FetchItem(
        path=Path("/src/b.md"),
        url=None,
        body="body b\n" * 10,
        source_hash="b" * 64,
        fetched_via="github",
    )
    result = run_batch_ingest(lib_with_git, "claude", Path("/src"), fetcher=_StaticFetcher([a, b]))
    assert result.created == 2

    conn = open_catalog(lib_with_git)
    try:
        rows = {p.slug: p.hash for p in list_pages(conn, section="library")}
    finally:
        conn.close()
    assert rows == {"claude/a": "a" * 64, "claude/b": "b" * 64}
