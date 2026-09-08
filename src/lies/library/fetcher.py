"""Concrete ``Fetcher`` that drives an existing scraper + format dispatch.

Bridges the abstract ``Fetcher`` protocol (Task 8's
``lies.library.ingest.Fetcher``) to the existing ``lies.scrapers`` and
``lies.etl.normalize.format_dispatch`` modules. The library is a sibling
of any wiki's data root, so this fetcher deliberately avoids the obsidian
frontmatter pass that requires a wiki ``Collection`` -- it produces a
clean markdown body that the ingest pipeline can hand to the mirror
writer, which applies the deterministic frontmatter with the upstream
``source_hash`` flowing into the ``ingested_at`` derivation.

Per-doc flow:

1. ``pick_scraper(source)`` selects a ``BaseScraper`` subclass.
2. ``scraper.fetch(source)`` returns raw bytes.
3. ``scraper.parse(raw, source=source)`` returns a list of ``ParsedDoc``.
4. For each ``ParsedDoc``, ``format_dispatch.dispatch`` produces a
   markdown body (markdown / html / rst / pdf formats handled; falls
   back to UTF-8 decode on error).
5. The upstream ``source_sha256`` (or ``sha256(content)`` when blank) is
   preserved on ``FetchItem.source_hash`` so the mirror writer can stamp
   it into the deterministic frontmatter.
6. Zero-emission scrapers raise ``LibraryFetchUnreachable`` at run
   boundary (Task 7's typed error), so the operator notices the empty
   batch instead of silently committing nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from lies.library.errors import LibraryFetchUnreachable
from lies.library.ingest import FetchItem
from lies.scrapers import base as _scraper_base
from lies.scrapers.base import ParsedDoc


def _hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalize_body(doc: ParsedDoc) -> str:
    """Best-effort markdown body without wiki/collection context.

    Uses ``format_dispatch.dispatch`` which is format-aware (markdown /
    html / rst / pdf) but skips the obsidian frontmatter pass that
    requires a wiki ``Collection``. Falls back to UTF-8 decode on any
    error so a single broken doc does not abort the whole batch.
    """
    try:
        from lies.etl.normalize.format_dispatch import dispatch

        return dispatch(doc.content, doc.source_format)
    except Exception:
        return doc.content.decode("utf-8", errors="replace")


class ScraperFetcher:
    """Concrete ``Fetcher`` driving an existing scraper + format dispatch.

    Module-attribute lookup (``_scraper_base.pick_scraper``) instead of a
    ``from ... import`` binding keeps ``monkeypatch.setattr`` on
    ``lies.scrapers.base.pick_scraper`` effective in tests.
    """

    def __init__(self, library: object | None) -> None:
        self._library = library

    def fetch_sources(self, source: Path | str) -> Iterator[FetchItem]:
        scraper = _scraper_base.pick_scraper(source)
        raw = scraper.fetch(source)
        docs = scraper.parse(raw, source=source)
        emitted = 0
        for doc in docs:
            source_hash = doc.source_sha256 or _hash_bytes(doc.content)
            yield FetchItem(
                path=Path(doc.path),
                url=str(source) if not isinstance(source, Path) else None,
                body=_normalize_body(doc),
                source_hash=source_hash,
                fetched_via=type(scraper).__name__,
            )
            emitted += 1
        if emitted == 0:
            raise LibraryFetchUnreachable(f"scraper produced 0 items for {source}")


__all__ = ("ScraperFetcher",)
