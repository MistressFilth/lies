"""PDFScraper — fetches a PDF document and parses pages into markdown chunks.

Text extraction via ``pymupdf``. Pages with no extractable text are
left to the normalize stage's OCR fallback.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf

from lies.scrapers.base import BaseScraper, ParsedDoc
from lies.scrapers.errors import ScraperFetchFailed


class PDFScraper(BaseScraper):
    def fetch(self, source: str | Path) -> bytes:
        path = Path(source)
        if not path.exists():
            raise ScraperFetchFailed(f"PDF not found: {path}")
        return path.read_bytes()

    def parse(self, raw: bytes) -> list[ParsedDoc]:
        doc = pymupdf.open(stream=raw, filetype="pdf")  # type: ignore[no-untyped-call]
        docs: list[ParsedDoc] = []
        for i, page in enumerate(doc):  # type: ignore[var-annotated,arg-type]
            text = page.get_text() or ""
            content = text.encode("utf-8")
            docs.append(
                ParsedDoc(
                    path=f"page-{i:04d}.md",
                    content=content,
                    source_sha256=hashlib.sha256(content).hexdigest(),
                    source_format="pdf",
                )
            )
        return docs

    def emit_manifest(self, docs: list[ParsedDoc], raw_dir: Path) -> Path:
        raw_dir.mkdir(parents=True, exist_ok=True)
        out = raw_dir / "manifest.json"
        payload = {"files": [{"path": d.path, "sha256": d.source_sha256} for d in docs]}
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return out
