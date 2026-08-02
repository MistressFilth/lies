"""SCRAPING stage — fetch + parse + emit manifest."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lies.collections.record import Collection
from lies.scrapers.base import pick_scraper

if TYPE_CHECKING:
    from lies.etl.pipeline import StageResult


def run_scrape(collection: Collection) -> StageResult:
    from lies.etl.pipeline import StageResult

    scraper = pick_scraper(collection.source)
    raw = scraper.fetch(collection.source)
    docs = scraper.parse(raw)
    raw_dir = collection.path
    raw_dir.mkdir(parents=True, exist_ok=True)
    scraper.emit_manifest(docs, raw_dir)
    return StageResult(
        success=[d.path for d in docs],
        quarantined=[],
        skipped=[],
        parsed_docs=docs,
        bytes_in=len(raw),
        bytes_out=0,
    )
