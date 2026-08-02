"""SCRAPING stage — fetch + parse + emit manifest.

Honors ``Collection.scraper_cmd`` for bespoke scrapers that live
outside the repo. ``scraper_cmd`` is a string of form
``module:attr`` or ``path/to/file.py:attr``; the attribute must
resolve to a ``BaseScraper`` instance. When unset, falls back to
``pick_scraper(collection.source)``.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from lies.collections.record import Collection
from lies.scrapers.base import BaseScraper, pick_scraper
from lies.scrapers.errors import ScraperUnavailable

if TYPE_CHECKING:
    from lies.etl.pipeline import StageResult


def _load_bespoke_scraper(scraper_cmd: str) -> BaseScraper:
    """Resolve ``module:attr`` or ``path.py:attr`` to a BaseScraper."""
    if ":" not in scraper_cmd:
        raise ScraperUnavailable(f"scraper_cmd must be 'module:attr', got: {scraper_cmd!r}")
    target, attr = scraper_cmd.rsplit(":", 1)
    if target.endswith(".py") and Path(target).exists():
        spec = importlib.util.spec_from_file_location(f"lies_bespoke_{attr}", target)
        if spec is None or spec.loader is None:
            raise ScraperUnavailable(f"could not load spec from {target!r}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ScraperUnavailable(f"could not import scraper module: {target}: {exc}") from exc
    else:
        try:
            module = importlib.import_module(target)
        except Exception as exc:
            raise ScraperUnavailable(f"could not import scraper module: {target}: {exc}") from exc
    try:
        scraper = getattr(module, attr)
    except AttributeError as exc:
        raise ScraperUnavailable(f"scraper module {target!r} has no attribute {attr!r}") from exc
    if not isinstance(scraper, BaseScraper):
        raise ScraperUnavailable(
            f"scraper {scraper_cmd!r} resolved to {type(scraper).__name__}, expected BaseScraper"
        )
    return scraper


def _resolve_scraper(collection: Collection) -> BaseScraper:
    if collection.scraper_cmd:
        return _load_bespoke_scraper(collection.scraper_cmd)
    return pick_scraper(collection.source)


def run_scrape(collection: Collection) -> StageResult:
    from lies.etl.pipeline import StageResult

    scraper = _resolve_scraper(collection)
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
