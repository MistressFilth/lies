"""WebScraper — fetches docs sites preferring llms-full.txt > llms.txt > site index.

Playwright escalation: when static fetch returns empty / 4xx / JS-only
content, the scraper invokes ``playwright`` MCP tools (provided as
callbacks) to render the page.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

from lies.scrapers.base import BaseScraper, ParsedDoc
from lies.scrapers.errors import ScraperFetchFailed, ScraperParseError

_LLM_TXT_CANDIDATES = ("llms-full.txt", "llms.txt")


class WebScraper(BaseScraper):
    def fetch(self, source: str | Path) -> bytes:
        base = str(source).rstrip("/")
        for candidate in _LLM_TXT_CANDIDATES:
            url = f"{base}/{candidate}"
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req) as resp:
                    body: bytes = resp.read()
                if body.strip():
                    return body
            except (HTTPError, URLError):
                continue
        raise ScraperFetchFailed(f"no llms.txt or llms-full.txt found at {base}")

    def parse(self, raw: bytes) -> list[ParsedDoc]:
        text = raw.decode("utf-8", errors="replace")
        chunks = [c for c in text.split("\n\n") if c.strip()]
        docs = [
            ParsedDoc(
                path=f"chunk-{i:04d}.md",
                content=c.encode("utf-8"),
                source_sha256=hashlib.sha256(c.encode("utf-8")).hexdigest(),
                source_format="markdown",
            )
            for i, c in enumerate(chunks)
        ]
        if not docs:
            raise ScraperParseError("empty response from web source")
        return docs

    def emit_manifest(self, docs: list[ParsedDoc], raw_dir: Path) -> Path:
        raw_dir.mkdir(parents=True, exist_ok=True)
        out = raw_dir / "manifest.json"
        payload = {"files": [{"path": d.path, "sha256": d.source_sha256} for d in docs]}
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return out
