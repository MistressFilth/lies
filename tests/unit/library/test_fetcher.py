"""Tests for ScraperFetcher (concrete Fetcher wrapping existing scrapers)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from lies.etl.normalize.format_dispatch import UnknownFormatError
from lies.library.errors import LibraryFetchUnreachable
from lies.library.fetcher import ScraperFetcher
from lies.library.ingest import FetchItem
from lies.scrapers.base import ParsedDoc


class _FakeScraper:
    """Fake scraper exposing the real ``fetch`` + ``parse`` interface."""

    def __init__(self, docs: list[ParsedDoc]) -> None:
        self._docs = docs
        self.fetch_called_with: list[object] = []
        self.parse_called_with: list[object] = []

    def fetch(self, source: Path | str) -> bytes:
        self.fetch_called_with.append(source)
        return b"raw bytes"

    def parse(
        self,
        raw: bytes,
        *,
        source: Path | str | None = None,
    ) -> list[ParsedDoc]:
        self.parse_called_with.append(source)
        return self._docs


def test_fetcher_invokes_local_file(monkeypatch, tmp_path: Path) -> None:
    """ScraperFetcher drives a fake scraper end-to-end and yields FetchItems.

    Covers the brief's primary happy-path: pick_scraper + fetch + parse +
    emit FetchItem with source_hash + fetched_via populated.
    """
    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.md",
                content=b"raw body",
                source_sha256="abc123def456",
                source_format="markdown",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources(src))

    assert len(items) == 1
    item = items[0]
    assert item.source_hash == "abc123def456"
    assert item.fetched_via == "_FakeScraper"
    assert item.body == "raw body"
    assert item.path == Path("page.md")
    # Source was a Path, not a URL — url stays None per FetchItem contract.
    assert item.url is None
    # Scraper's fetch + parse were both invoked with the source.
    assert fake.fetch_called_with == [src]
    assert fake.parse_called_with == [src]


def test_fetcher_url_source_sets_url_field(monkeypatch) -> None:
    """When source is a str (URL), ``FetchItem.url`` carries it.

    ``path`` is still derived from the parsed doc's relative path. Mirrors
    the existing wiki-side ingest semantics where URL fetches leave path
    empty and route via URL instead.
    """
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="chunk-0000.md",
                content=b"hello",
                source_sha256="deadbeef",
                source_format="markdown",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources("https://example.com/page"))

    assert len(items) == 1
    assert items[0].url == "https://example.com/page"
    assert items[0].path == Path("chunk-0000.md")


def test_fetcher_emits_multiple_docs(monkeypatch) -> None:
    """Scrapers yielding multiple docs → multiple FetchItems.

    E.g. an llms.txt index page produces one item per linked page.
    """
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="a.md",
                content=b"aaa",
                source_sha256="aa" * 32,
                source_format="markdown",
            ),
            ParsedDoc(
                path="b.md",
                content=b"bbb",
                source_sha256="bb" * 32,
                source_format="markdown",
            ),
            ParsedDoc(
                path="c.md",
                content=b"ccc",
                source_sha256="cc" * 32,
                source_format="markdown",
            ),
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources("https://example.com/x"))

    assert [i.path.name for i in items] == ["a.md", "b.md", "c.md"]
    assert [i.source_hash for i in items] == ["aa" * 32, "bb" * 32, "cc" * 32]


def test_fetcher_derives_hash_when_scraper_omits_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """ParsedDoc.source_sha256 may be empty; fetcher falls back to sha256(content)."""
    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    content = b"\x00\x01\x02 deterministic bytes"
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.md",
                content=content,
                source_sha256="",  # blank — fetcher must compute
                source_format="markdown",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources(src))

    import hashlib

    assert items[0].source_hash == hashlib.sha256(content).hexdigest()


def test_fetcher_zero_items_raises_library_fetch_unreachable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Zero-emission scraper → ``LibraryFetchUnreachable`` (Task 7 error)."""
    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    fake = _FakeScraper([])
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    with pytest.raises(LibraryFetchUnreachable):
        list(fetcher.fetch_sources(src))


def test_fetcher_yields_iterator_not_list(monkeypatch, tmp_path: Path) -> None:
    """Brief contract: ``fetch_sources`` returns an ``Iterator[FetchItem]``.

    Generator semantics matter so the ingest pipeline can stream large
    batches without materializing them.
    """
    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.md",
                content=b"hi",
                source_sha256="ff" * 32,
                source_format="markdown",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    result = fetcher.fetch_sources(src)
    assert isinstance(result, Iterator)
    # Pulling consumes the generator — confirm items flow before exhaustion.
    first = next(result)
    assert isinstance(first, FetchItem)


def test_fetcher_unknown_format_propagates(monkeypatch, tmp_path: Path) -> None:
    """A scraper emitting ``source_format="liquid"`` propagates ``UnknownFormatError``.

    The fetcher must not swallow dispatch failures into a UTF-8 decode
    fallback: a PDF or pandoc outage must not land raw binary as if it
    were clean markdown. The ingest pipeline handles quarantine.
    """
    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.md",
                content=b"<html>raw html bytes</html>",
                source_sha256="aa" * 32,
                source_format="liquid",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    with pytest.raises(UnknownFormatError):
        list(fetcher.fetch_sources(src))


def test_fetcher_unrecognized_format_propagates(monkeypatch, tmp_path: Path) -> None:
    """An arbitrary unknown source_format propagates ``UnknownFormatError``.

    Guards the catch-all branch of ``format_dispatch.dispatch`` (anything
    not in the markdown/html/rst/pdf/liquid whitelist raises). Without
    this propagation, raw content could silently land as a mirror file.
    """
    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.md",
                content=b"\x00\x01\x02 binary blob",
                source_sha256="bb" * 32,
                source_format="x-totally-made-up",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    with pytest.raises(UnknownFormatError):
        list(fetcher.fetch_sources(src))
