"""The PDF builder against real PDF bytes, not a stub."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lies.builders.errors import BuilderParseError
from lies.builders.pdf import PDFBuilder

# Comfortably past the ingest-side thin-content guard, so a page that
# renders correctly here is not silently quarantined downstream.
_PROSE = (
    "Body prose that is comfortably long enough to clear the thin-content "
    "guard on the ingest side of the pipeline."
)


def _real_pdf(path: Path, *, pages: int = 3, text: bool = True) -> Path:
    """Write a real, structurally complete PDF and return its path.

    ``pymupdf`` writes the container: version header, compressed content
    streams, cross-reference table and font resources. With ``text=False``
    the pages carry geometry but no text operators, which is the closest
    stand-in for a scanned page available without shipping an image.
    """
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        if text:
            page.insert_text((72, 100), f"Page {i + 1} heading for the fixture.", fontsize=14)
            page.insert_text((72, 130), _PROSE, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


class _Collection:
    """The builder reads nothing off the collection beyond being handed one."""

    name = "pdffix"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _build(workspace: Path, pdf: Path) -> list[Any]:
    import shutil

    shutil.copyfile(pdf, workspace / "source.pdf")
    return PDFBuilder().build(workspace, collection=_Collection())  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# A real document with a text layer
# ---------------------------------------------------------------------


def test_a_real_pdf_yields_one_document_per_page(tmp_path: Path, workspace: Path) -> None:
    """Three pages in, three documents out."""
    pdf = _real_pdf(tmp_path / "real.pdf", pages=3)

    docs = _build(workspace, pdf)

    assert len(docs) == 3, f"expected one document per page; got {len(docs)}"
    assert [d.path for d in docs] == [
        "pages/page-0001.md",
        "pages/page-0002.md",
        "pages/page-0003.md",
    ]


def test_the_extracted_text_is_the_documents_own(tmp_path: Path, workspace: Path) -> None:
    """Real text comes out, per page, and it is that page's text."""
    pdf = _real_pdf(tmp_path / "real.pdf", pages=3)

    docs = _build(workspace, pdf)

    for i, doc in enumerate(docs):
        text = doc.content.decode("utf-8")
        assert f"Page {i + 1} heading" in text, (
            f"page {i + 1} carries another page's text: {text[:120]!r}"
        )
        assert _PROSE[:40] in text, f"page {i + 1} lost its body: {text[:120]!r}"


def test_each_page_is_hashed_from_its_own_content(tmp_path: Path, workspace: Path) -> None:
    """Per-page hashes, distinct across pages."""
    import hashlib

    pdf = _real_pdf(tmp_path / "real.pdf", pages=3)

    docs = _build(workspace, pdf)

    hashes = [d.source_sha256 for d in docs]
    assert len(set(hashes)) == len(hashes), f"pages share a hash: {hashes}"
    for doc in docs:
        expected = hashlib.sha256(doc.content).hexdigest()
        assert doc.source_sha256 == expected, (
            "the hash must cover the emitted page, not the source PDF"
        )


def test_the_output_is_markdown_for_the_downstream_filter(tmp_path: Path, workspace: Path) -> None:
    """``source_format`` is what routes the page to the markdown filter."""
    pdf = _real_pdf(tmp_path / "real.pdf", pages=1)

    docs = _build(workspace, pdf)

    assert docs[0].source_format == "markdown"


# ---------------------------------------------------------------------
# The fallback path
# ---------------------------------------------------------------------


def test_a_pdf_with_no_text_layer_does_not_raise(tmp_path: Path, workspace: Path) -> None:
    """The pymupdf fallback is reached and the page still yields a document."""
    pdf = _real_pdf(tmp_path / "blank.pdf", pages=2, text=False)

    docs = _build(workspace, pdf)

    assert len(docs) == 2, f"a text-less page must still yield a document; got {len(docs)}"
    for doc in docs:
        assert isinstance(doc.content, bytes)
        assert doc.content.decode("utf-8").strip() == "", (
            "no text operators means no extractable text, under either extractor"
        )


def test_a_missing_source_pdf_is_a_builder_error(workspace: Path) -> None:
    """The precondition still fails loudly."""
    with pytest.raises(BuilderParseError):
        PDFBuilder().build(workspace, collection=_Collection())  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# The scraper half
# ---------------------------------------------------------------------


def test_the_scraper_preserves_the_bytes_verbatim(tmp_path: Path) -> None:
    """``PDFScraper.parse`` hands the builder exactly what it fetched."""
    import hashlib

    from lies.scrapers.pdf import PDFScraper

    pdf = _real_pdf(tmp_path / "real.pdf", pages=2)
    raw = pdf.read_bytes()

    docs = PDFScraper().parse(raw, source=pdf)

    assert len(docs) == 1
    assert docs[0].content == raw, "the scraper must not transform the bytes"
    assert docs[0].source_sha256 == hashlib.sha256(raw).hexdigest()
    assert docs[0].source_format == "pdf"


def test_a_missing_pdf_is_a_scraper_error(tmp_path: Path) -> None:
    """A path that does not exist fails at fetch, not at build."""
    from lies.scrapers.errors import ScraperFetchFailed
    from lies.scrapers.pdf import PDFScraper

    with pytest.raises(ScraperFetchFailed):
        PDFScraper().fetch(tmp_path / "nope.pdf")


def test_the_two_halves_agree_on_a_real_document(tmp_path: Path, workspace: Path) -> None:
    """Scraper → builder, the actual pipeline, on real bytes."""

    from lies.scrapers.pdf import PDFScraper

    pdf = _real_pdf(tmp_path / "real.pdf", pages=2)
    scraped = PDFScraper().parse(pdf.read_bytes(), source=pdf)

    ws = workspace / "materialised"
    ws.mkdir()
    (ws / scraped[0].path).write_bytes(scraped[0].content)

    docs = PDFBuilder().build(ws, collection=_Collection())  # type: ignore[arg-type]

    assert len(docs) == 2
    assert all(b"heading" in d.content for d in docs)
