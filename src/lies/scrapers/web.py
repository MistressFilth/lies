"""WebScraper — fetches docs from a website with llms.txt preference."""
from __future__ import annotations

from lies.scrapers.base import BaseScraper


class WebScraper(BaseScraper):
    def fetch(self, source):  # type: ignore[no-untyped-def]
        pass

    def parse(self, raw):  # type: ignore[no-untyped-def]
        pass

    def emit_manifest(self, docs, raw_dir):  # type: ignore[no-untyped-def]
        pass

