from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lies.collections.record import Collection
from lies.etl.stages.normalize import run_normalize
from lies.etl.stages.qmd_update import run_qmd_update
from lies.etl.stages.scrape import run_scrape
from lies.etl.stages.write import run_write
from lies.scrapers.base import ParsedDoc


def _collection(tmp_path: Path) -> Collection:
    return Collection(
        name="cpython",
        path=tmp_path / "raw" / "cpython",
        source="https://example.com",
        tags=[], scraper_cmd=None, doc_path=None,
        mapper_model=None, language=None, version="1.0.0",
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


def test_run_scrape_invokes_pick_scraper(tmp_path: Path) -> None:
    fake_scraper = mock.Mock()
    fake_scraper.fetch.return_value = b"# hello"
    fake_docs = [
        ParsedDoc(path="x.md", content=b"# hello", source_sha256="abc", source_format="markdown"),
    ]
    fake_scraper.parse.return_value = fake_docs
    fake_scraper.emit_manifest.return_value = tmp_path / "raw" / "cpython" / "manifest.json"
    collection = _collection(tmp_path)
    with mock.patch("lies.etl.stages.scrape.pick_scraper", return_value=fake_scraper) as mock_pick:
        result = run_scrape(collection)
    mock_pick.assert_called_once_with(collection.source)
    fake_scraper.fetch.assert_called_once_with(collection.source)
    fake_scraper.parse.assert_called_once_with(b"# hello")
    fake_scraper.emit_manifest.assert_called_once_with(fake_docs, collection.path)
    assert collection.path.is_dir()
    assert result.success == ["x.md"]
    assert result.parsed_docs == fake_docs
    assert result.bytes_in == len(b"# hello")


def test_run_normalize_dispatches_and_applies_obsidian(tmp_path: Path) -> None:
    fake_doc = ParsedDoc(path="x.md", content=b"# Hello", source_sha256="abc", source_format="markdown")
    with mock.patch("lies.etl.stages.normalize.format_dispatch.dispatch", return_value="# Hello"), \
         mock.patch("lies.etl.stages.normalize.obsidian.apply", side_effect=lambda m, **kw: m) as obs:
        result = run_normalize(_collection(tmp_path), [fake_doc])
    assert result.success == ["x.md"]
    obs.assert_called_once()
    # normalize stage emits parsed_docs carrying the markdown
    assert len(result.parsed_docs) == 1


def test_run_normalize_quarantines_unknown_format(tmp_path: Path) -> None:
    from lies.etl.normalize.format_dispatch import UnknownFormatError
    fake_doc = ParsedDoc(path="bad.xyz", content=b"x", source_sha256="abc", source_format="weird")
    with mock.patch(
        "lies.etl.stages.normalize.format_dispatch.dispatch",
        side_effect=UnknownFormatError("nope"),
    ):
        result = run_normalize(_collection(tmp_path), [fake_doc])
    assert result.quarantined == [("bad.xyz", "nope")]


def test_run_write_atomic_commits_new_files(tmp_path: Path) -> None:
    fake_normalized = [("x.md", "# body")]
    manifest = mock.Mock()
    manifest.compare.return_value = False  # fresh manifest: no prior entries
    with mock.patch("lies.etl.stages.write.atomic_commit") as ac:
        result = run_write(_collection(tmp_path), fake_normalized,
                           manifest=manifest, wiki_root=tmp_path, force=False)
    assert result.success == ["x.md"]
    ac.assert_called_once()
    written = tmp_path / "wiki" / "x.md"
    assert written.exists()


def test_run_write_writes_under_wiki_root(tmp_path: Path) -> None:
    """Target path is wiki_root/wiki/<path>, not CWD-relative."""
    fake_normalized = [("concepts/example.md", "# body")]
    manifest = mock.Mock()
    manifest.compare.return_value = False
    with mock.patch("lies.etl.stages.write.atomic_commit"):
        run_write(_collection(tmp_path), fake_normalized,
                  manifest=manifest, wiki_root=tmp_path, force=False)
    assert (tmp_path / "wiki" / "concepts" / "example.md").exists()


def test_run_write_skips_unchanged(tmp_path: Path) -> None:
    manifest = mock.Mock()
    manifest.compare.return_value = True
    fake_normalized = [("x.md", "# body")]
    with mock.patch("lies.etl.stages.write.atomic_commit") as ac:
        result = run_write(_collection(tmp_path), fake_normalized,
                           manifest=manifest, wiki_root=tmp_path, force=False)
    assert result.skipped == ["x.md"]
    ac.assert_not_called()


def test_run_write_quarantines_on_oserror(tmp_path: Path) -> None:
    fake_normalized = [("x.md", "# body")]
    manifest = mock.Mock()
    manifest.compare.return_value = False
    with mock.patch("lies.etl.stages.write.atomic_commit"), \
         mock.patch("builtins.open", side_effect=OSError("disk full")):
        result = run_write(_collection(tmp_path), fake_normalized,
                           manifest=manifest, wiki_root=tmp_path, force=False)
    assert result.quarantined == [("x.md", "disk full")]


def test_run_write_respects_force(tmp_path: Path) -> None:
    manifest = mock.Mock()
    manifest.compare.return_value = True
    fake_normalized = [("x.md", "# body")]
    with mock.patch("lies.etl.stages.write.atomic_commit") as ac:
        result = run_write(_collection(tmp_path), fake_normalized,
                           manifest=manifest, wiki_root=tmp_path, force=True)
    assert result.success == ["x.md"]
    assert result.skipped == []
    ac.assert_called_once()


def test_run_qmd_update_calls_incremental(tmp_path: Path) -> None:
    with mock.patch("lies.etl.stages.qmd_update.qmd_update") as q:
        result = run_qmd_update(_collection(tmp_path))
    q.assert_called_once_with(tmp_path / "raw" / "cpython", collection="cpython")
    assert result.bytes_in == 0
    assert result.success == []


def test_run_qmd_update_swallows_qmd_failure(tmp_path: Path) -> None:
    with mock.patch("lies.etl.stages.qmd_update.qmd_update", side_effect=RuntimeError("qmd missing")):
        result = run_qmd_update(_collection(tmp_path))
    assert result.bytes_in == 0  # no-op recorded
