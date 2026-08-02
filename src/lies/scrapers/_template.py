"""Scraper template for LLM-driven scraper generation."""

from __future__ import annotations

from lies.scrapers.base import BaseScraper


class GeneratedScraper(BaseScraper):
    """Template subclass. LLM fills in fetch/parse/emit_manifest."""

    def fetch(self, source):  # type: ignore[no-untyped-def]
        pass

    def parse(self, raw):  # type: ignore[no-untyped-def]
        pass

    def emit_manifest(self, docs, raw_dir):  # type: ignore[no-untyped-def]
        pass
