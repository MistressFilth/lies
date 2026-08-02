"""PDF text extraction (pymupdf) + OCR fallback (tesseract)."""

from __future__ import annotations

import subprocess

import pymupdf


def extract_text(pdf_bytes: bytes) -> str:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")  # type: ignore[no-untyped-call]
    parts: list[str] = []
    for page in doc:  # type: ignore[attr-defined]
        text = page.get_text() or ""
        parts.append(text)
    return "\n\n".join(parts)


def extract_text_ocr(pdf_bytes: bytes) -> str:
    """Render PDF pages to images and OCR each via tesseract."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")  # type: ignore[no-untyped-call]
    parts: list[str] = []
    for page in doc:  # type: ignore[attr-defined]
        pix = page.get_pixmap(dpi=200)
        png_bytes = pix.tobytes("png")
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout"],
            input=png_bytes,
            capture_output=True,
            check=True,
        )
        parts.append(proc.stdout.decode("utf-8", errors="replace"))
    return "\n\n".join(parts)
