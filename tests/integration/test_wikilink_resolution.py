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


def test_real_repo_wikilink_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrape a small real repo (g-Research/ahocorasick_rs) and verify the
    corpus build matches the on-disk markdown count."""
    from lies.scrapers.github import GitHubScraper  # type: ignore[import-not-found]

    # ``_isolated_xdg`` in ``tests/conftest.py`` redirects
    # ``XDG_CONFIG_HOME`` to ``tmp_path/xdg/config``, which makes the
    # ``gh`` CLI fail with "To get started with GitHub CLI, please run:
    # gh auth login" because it cannot find its auth config. Unset it
    # for the duration of the test so ``gh`` falls back to
    # ``~/.config/gh/hosts.yml``.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "wiki").mkdir()
    out_dir = wiki_root / "raw" / "ahocorasick_rs"
    out_dir.mkdir(parents=True)

    scraper = GitHubScraper()  # type: ignore[no-untyped-call]
    raw_bytes = scraper.fetch("https://github.com/g-Research/ahocorasick_rs")
    docs = scraper.parse(raw_bytes)
    for doc in docs:
        target = out_dir / doc.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(doc.content)
    scraper.emit_manifest(docs, out_dir)

    resolver = WikiLinkResolver.build((wiki_root / "wiki", wiki_root / "raw"))
    wiki_pages = list((wiki_root / "wiki").rglob("*.md"))
    raw_pages = list((wiki_root / "raw").rglob("*.md"))
    # Every scraped page should be addressable by its lowercase stem at minimum.
    for path in raw_pages:
        assert resolver.resolve(path.stem) == path.resolve()
    # The corpus must include every page on disk.
    assert len(resolver._keys) >= len(wiki_pages) + len(raw_pages)
