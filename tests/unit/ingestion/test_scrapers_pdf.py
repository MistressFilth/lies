import hashlib
import json
from pathlib import Path

import pytest

from lies.scrapers.base import ParsedDoc
from lies.scrapers.errors import ScraperFetchFailed
from lies.scrapers.pdf import PDFScraper


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_pdf_scraper_fetches_file(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-fake")
    body = PDFScraper().fetch(pdf)
    assert body == b"%PDF-fake"


def test_pdf_scraper_fetch_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(ScraperFetchFailed):
        PDFScraper().fetch(tmp_path / "missing.pdf")


def test_pdf_scraper_parse_preserves_source_bytes_for_builder() -> None:
    raw = b"%PDF-fake"
    docs = PDFScraper().parse(raw)
    assert len(docs) == 1
    assert docs[0].path == "source.pdf"
    assert docs[0].content == raw
    assert docs[0].source_format == "pdf"


def test_pdf_scraper_emits_manifest(tmp_path: Path) -> None:
    docs = [
        ParsedDoc(path="x.md", content=b"# x", source_sha256=_sha("# x"), source_format="markdown")
    ]
    out = PDFScraper().emit_manifest(docs, tmp_path)
    assert json.loads(out.read_text(encoding="utf-8"))["files"]
