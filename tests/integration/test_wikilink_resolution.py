"""Integration tests for WikiLink resolution against a real scraped wiki.

Gated on ``INTEGRATION=1``. Network access required.

Run: ``INTEGRATION=1 uv run pytest tests/integration/test_wikilink_resolution.py -v``
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lies.wikilinks import WikiLinkResolver

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("INTEGRATION") != "1",
        reason="set INTEGRATION=1 to run integration tests (network required)",
    ),
]


def test_real_repo_wikilink_corpus(tmp_path: Path) -> None:
    """Scrape a small real repo (g-Research/ahocorasick_rs) and verify the
    corpus build matches the on-disk markdown count."""
    from lies.scrapers.github import GitHubScraper  # type: ignore[import-not-found]

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "wiki").mkdir()
    (wiki_root / "raw").mkdir()

    scraper = GitHubScraper(  # type: ignore[no-untyped-call]
        source="https://github.com/g-Research/ahocorasick_rs",
        out_root=wiki_root / "raw" / "ahocorasick_rs",
    )
    scraper.fetch()

    resolver = WikiLinkResolver.build((wiki_root / "wiki", wiki_root / "raw"))
    wiki_pages = list((wiki_root / "wiki").rglob("*.md"))
    raw_pages = list((wiki_root / "raw").rglob("*.md"))
    # Every scraped page should be addressable by its lowercase stem at minimum.
    for path in raw_pages:
        assert resolver.resolve(path.stem) == path.resolve()
    # The corpus must include every page on disk.
    assert len(resolver._keys) >= len(wiki_pages) + len(raw_pages)
