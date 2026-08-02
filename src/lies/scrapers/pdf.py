"""PDFScraper — fetches a PDF document as builder input.

The scraper preserves the fetched bytes and labels them ``pdf``. The
NORMALIZE stage hands those bytes to ``PDFBuilder``, which performs
page-level text extraction and emits markdown documents.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lies.scrapers.base import BaseScraper, ParsedDoc
from lies.scrapers.errors import ScraperFetchFailed


class PDFScraper(BaseScraper):
    def fetch(self, source: str | Path) -> bytes:
        path = Path(source)
        if not path.exists():
            raise ScraperFetchFailed(f"PDF not found: {path}")
        return path.read_bytes()

    def parse(self, raw: bytes) -> list[ParsedDoc]:
        """Return the fetched PDF unchanged for ``PDFBuilder``."""
        return [
            ParsedDoc(
                path="source.pdf",
                content=raw,
                source_sha256=hashlib.sha256(raw).hexdigest(),
                source_format="pdf",
            )
        ]

    def emit_manifest(self, docs: list[ParsedDoc], raw_dir: Path) -> Path:
        raw_dir.mkdir(parents=True, exist_ok=True)
        out = raw_dir / "manifest.json"
        payload = {"files": [{"path": d.path, "sha256": d.source_sha256} for d in docs]}
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return out
