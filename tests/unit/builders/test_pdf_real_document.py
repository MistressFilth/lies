"""The PDF builder against real PDF bytes, not a stub.

``PDFBuilder`` shipped in 0.48.1 and had never been run against an actual
PDF. Every test that touched it fed it a hand-written ``ParsedDoc``, so
the whole chain — ``PDFScraper.fetch`` reading bytes off disk,
``PDFBuilder`` opening them with pdfplumber, page iteration, text
extraction, the pymupdf fallback — was unexercised. A builder that
raises on every real document and passes every mocked one is exactly
what that gap hides.

The fixture is generated with ``pymupdf``, which is already a declared
dependency, so the document is real rather than assembled by hand: a
``%PDF-1.7`` header, a compressed content stream, an xref table, and an
embedded font subset that pdfplumber has to decompress and map. No
network fetch, no binary committed to the repository, and no
third-party document whose licence the repository would inherit.

**What this does and does not cover.** It covers the ordinary
text-layer document, which is what the pipeline is pointed at. It does
not cover a scanned image-only PDF, an exotic font encoding, or
encrypted files — those need documents this repository has no right to
redistribute, and they remain untested rather than pretended at.
"""

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
    """Three pages in, three documents out.

    The page count is what makes this a test rather than a smoke check: a
    builder that emitted one document for the whole file would still pass
    a single-page assertion, and the page boundary is the entire reason
    ``PDFBuilder`` exists.
    """
    pdf = _real_pdf(tmp_path / "real.pdf", pages=3)

    docs = _build(workspace, pdf)

    assert len(docs) == 3, f"expected one document per page; got {len(docs)}"
    assert [d.path for d in docs] == [
        "pages/page-0001.md",
        "pages/page-0002.md",
        "pages/page-0003.md",
    ]


def test_the_extracted_text_is_the_documents_own(tmp_path: Path, workspace: Path) -> None:
    """Real text comes out, per page, and it is that page's text.

    A builder returning the first page's text on every page would pass a
    length or count assertion, so each page is asserted for content
    unique to it.
    """
    pdf = _real_pdf(tmp_path / "real.pdf", pages=3)

    docs = _build(workspace, pdf)

    for i, doc in enumerate(docs):
        text = doc.content.decode("utf-8")
        assert f"Page {i + 1} heading" in text, (
            f"page {i + 1} carries another page's text: {text[:120]!r}"
        )
        assert _PROSE[:40] in text, f"page {i + 1} lost its body: {text[:120]!r}"


def test_each_page_is_hashed_from_its_own_content(tmp_path: Path, workspace: Path) -> None:
    """Per-page hashes, distinct across pages.

    ``source_sha256`` is what the idempotency check compares on a
    re-ingest. If every page hashed the same bytes, a re-run would read
    as "unchanged" for all of them and a genuine edit to page 2 would
    never be noticed.
    """
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
    """``source_format`` is what routes the page to the markdown filter.

    ``source_format="markdown"`` on a PDF-derived page is deliberate —
    the bytes are already markdown by then — and getting it wrong sends
    every page down the PDF branch of the dispatcher a second time.
    """
    pdf = _real_pdf(tmp_path / "real.pdf", pages=1)

    docs = _build(workspace, pdf)

    assert docs[0].source_format == "markdown"


# ---------------------------------------------------------------------
# The fallback path
# ---------------------------------------------------------------------


def test_a_pdf_with_no_text_layer_does_not_raise(tmp_path: Path, workspace: Path) -> None:
    """The pymupdf fallback is reached and the page still yields a document.

    ``_pdfplumber_page`` returns ``""`` for a page with no text
    operators, and the builder then calls ``_pymupdf_page``. With no
    text in the file both extractors return nothing — so this asserts the
    *fallback is reached and does not crash*, not that it recovers text.
    That is the honest claim: a genuinely scanned document is out of
    scope for this repository to fixture, and claiming otherwise here
    would be a test that asserts the absence of a defect.
    """
    pdf = _real_pdf(tmp_path / "blank.pdf", pages=2, text=False)

    docs = _build(workspace, pdf)

    assert len(docs) == 2, f"a text-less page must still yield a document; got {len(docs)}"
    for doc in docs:
        assert isinstance(doc.content, bytes)
        assert doc.content.decode("utf-8").strip() == "", (
            "no text operators means no extractable text, under either extractor"
        )


def test_a_missing_source_pdf_is_a_builder_error(workspace: Path) -> None:
    """The precondition still fails loudly.

    Without ``source.pdf`` the builder must raise ``BuilderParseError``,
    not return an empty list — an empty list is indistinguishable from a
    successful parse of a document that had nothing on it.
    """
    with pytest.raises(BuilderParseError):
        PDFBuilder().build(workspace, collection=_Collection())  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# The scraper half
# ---------------------------------------------------------------------


def test_the_scraper_preserves_the_bytes_verbatim(tmp_path: Path) -> None:
    """``PDFScraper.parse`` hands the builder exactly what it fetched.

    ``source_sha256`` is taken over the raw bytes here and over the
    rendered page in the builder, so the two hashes are answering
    different questions on purpose. Hashing the *rendered* text in the
    scraper would make every re-fetch of an unchanged document look
    changed whenever the extractor version moved.
    """
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
    """Scraper → builder, the actual pipeline, on real bytes.

    The halves are tested separately above because a failure in either
    is easier to read that way. This one runs them together, because the
    contract between them — the builder looks for exactly
    ``source.pdf`` in a workspace the caller materialises from the
    scraper's ``path`` — is a naming agreement between two modules that
    no single-file test can see.
    """

    from lies.scrapers.pdf import PDFScraper

    pdf = _real_pdf(tmp_path / "real.pdf", pages=2)
    scraped = PDFScraper().parse(pdf.read_bytes(), source=pdf)

    ws = workspace / "materialised"
    ws.mkdir()
    (ws / scraped[0].path).write_bytes(scraped[0].content)

    docs = PDFBuilder().build(ws, collection=_Collection())  # type: ignore[arg-type]

    assert len(docs) == 2
    assert all(b"heading" in d.content for d in docs)
