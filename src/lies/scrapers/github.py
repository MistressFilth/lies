"""GitHubScraper — fetches docs from a GitHub repo via MCP/CLI/sparse clone."""
from __future__ import annotations

from lies.scrapers.base import BaseScraper


class GitHubScraper(BaseScraper):
    def fetch(self, source):  # type: ignore[no-untyped-def]
        pass

    def parse(self, raw):  # type: ignore[no-untyped-def]
        pass

    def emit_manifest(self, docs, raw_dir):  # type: ignore[no-untyped-def]
        pass

