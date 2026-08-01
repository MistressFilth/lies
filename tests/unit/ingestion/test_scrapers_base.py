from dataclasses import dataclass
from pathlib import Path

import pytest

from lies.scrapers.base import BaseScraper, ParsedDoc, pick_scraper
from lies.scrapers.errors import ScraperUnavailable


@dataclass
class _StubScraper(BaseScraper):
    CONTRACT_VERSION: int = 1
    marker: str = ""

    def fetch(self, source):  # type: ignore[no-untyped-def]
        return self.marker.encode("utf-8")

    def parse(self, raw: bytes) -> list[ParsedDoc]:
        return [ParsedDoc(path="x.md", content=raw, source_sha256="", source_format="markdown")]

    def emit_manifest(self, docs, raw_dir: Path) -> Path:  # type: ignore[no-untyped-def]
        return raw_dir / "manifest.json"


def test_base_scraper_contract_version_default() -> None:
    assert BaseScraper.CONTRACT_VERSION == 1


def test_pick_scraper_matches_github_url() -> None:
    from lies.scrapers import github as _gh

    scraper = pick_scraper("https://github.com/python/cpython")
    assert isinstance(scraper, _gh.GitHubScraper)


def test_pick_scraper_unknown_raises() -> None:
    with pytest.raises(ScraperUnavailable):
        pick_scraper("not a real source")
