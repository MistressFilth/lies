"""Base scraper contract."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from lies.scrapers.errors import ScraperUnavailable


@dataclass(frozen=True)
class ParsedDoc:
    path: str  # relative to raw/<collection>/
    content: bytes
    source_sha256: str
    source_format: str  # markdown | html | rst | pdf | liquid


class BaseScraper(ABC):
    CONTRACT_VERSION: ClassVar[int] = 1

    @abstractmethod
    def fetch(self, source: str | Path) -> bytes: ...

    @abstractmethod
    def parse(self, raw: bytes) -> list[ParsedDoc]: ...

    @abstractmethod
    def emit_manifest(self, docs: list[ParsedDoc], raw_dir: Path) -> Path: ...


def pick_scraper(source: str | Path) -> BaseScraper:
    """Select the right scraper subclass for a source."""
    from lies.scrapers.github import GitHubScraper
    from lies.scrapers.pdf import PDFScraper
    from lies.scrapers.web import WebScraper

    s = str(source)
    if s.startswith(("https://github.com/", "git@github.com:")):
        return GitHubScraper()
    if s.startswith(("http://", "https://")):
        return WebScraper()
    p = Path(s)
    if p.suffix.lower() == ".pdf":
        return PDFScraper()
    if p.exists() and p.is_file():
        if p.suffix.lower() == ".pdf":
            return PDFScraper()
        return WebScraper()
    raise ScraperUnavailable(f"no scraper matches source: {source}")
