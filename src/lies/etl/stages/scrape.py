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
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from lies.collections.record import Collection
from lies.scrapers.base import BaseScraper, pick_scraper
from lies.scrapers.errors import ScraperUnavailable
from lies.wiki.wiki import Wiki

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


def run_scrape(wiki: Wiki, collection: Collection) -> StageResult:
    from lies.etl.pipeline import StageResult

    scraper = _resolve_scraper(collection)
    raw = scraper.fetch(collection.source)
    raw_dir = wiki.raw_dir / collection.name
    raw_dir.mkdir(parents=True, exist_ok=True)
    emitted_manifest = scraper.emit_manifest(docs := scraper.parse(raw), raw_dir)

    # Scrapers still receive the raw directory so bespoke implementations can
    # emit their source files there. Keep the manifest beside the other cache
    # artifacts instead of leaving derived state in the wiki data repository.
    manifest_path = wiki.cache_root / "collections" / collection.name / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if emitted_manifest.exists():
        if emitted_manifest.resolve() != manifest_path.resolve():
            shutil.copyfile(emitted_manifest, manifest_path)
            emitted_manifest.unlink()
    else:
        # When a scraper does not emit a manifest (e.g., bespoke raw
        # sources), make sure the canonical location exists so downstream
        # consumers can rely on the path.
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        if not manifest_path.exists():
            manifest_path.write_text('{"files": []}', encoding="utf-8")
    return StageResult(
        success=[d.path for d in docs],
        quarantined=[],
        skipped=[],
        parsed_docs=docs,
        bytes_in=len(raw),
        bytes_out=0,
    )
