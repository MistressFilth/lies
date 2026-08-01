from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lies.collections.record import Collection
from lies.etl.stages.normalize import run_normalize
from lies.etl.stages.scrape import run_scrape
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
