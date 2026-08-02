"""PDF source-format builder.

Iterates pages. Per page, attempt ``pdfplumber.Page.extract_text()``;
on empty result (table-only or scanned), fall back to
``pymupdf.Page.get_text()``. One ``ParsedDoc`` per page with
``path="pages/page-NNNN.md"`` and ``source_format="markdown"``.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pdfplumber
import pymupdf

from lies.builders.base import REGISTRY, Builder
from lies.builders.errors import BuilderParseError
from lies.collections.record import Collection
from lies.scrapers.base import ParsedDoc


def _page_path(index_zero_based: int) -> str:
    return f"pages/page-{index_zero_based + 1:04d}.md"


def _pdfplumber_page(pdf: Any, index_zero_based: int) -> str:
    page = pdf.pages[index_zero_based]
    return page.extract_text() or ""


def _pymupdf_page(path: Path, index_zero_based: int) -> str:
    doc = pymupdf.open(str(path))  # type: ignore[no-untyped-call]
    try:
        text = doc[index_zero_based].get_text()  # type: ignore[no-untyped-call]
        return text if isinstance(text, str) else str(text)
    finally:
        doc.close()  # type: ignore[no-untyped-call]


class PDFBuilder(Builder):
    def build(self, workspace: Path, *, collection: Collection) -> list[ParsedDoc]:
        src = workspace / "source.pdf"
        if not src.exists():
            raise BuilderParseError("source.pdf missing", path=str(src))
        try:
            with pdfplumber.open(str(src)) as pdf:
                page_count = len(pdf.pages)
                primary = [_pdfplumber_page(pdf, i) for i in range(page_count)]
        except Exception as exc:  # pdfplumber raises a mix of exceptions
            raise BuilderParseError(f"pdfplumber: {exc}", path=str(src)) from exc
        out: list[ParsedDoc] = []
        for i, text in enumerate(primary):
            if not text.strip():
                text = _pymupdf_page(src, i)
            md = text.strip() + "\n"
            out.append(
                ParsedDoc(
                    path=_page_path(i),
                    content=md.encode("utf-8"),
                    source_sha256=hashlib.sha256(md.encode("utf-8")).hexdigest(),
                    source_format="markdown",
                )
            )
        return out


REGISTRY.register("pdf", PDFBuilder())
