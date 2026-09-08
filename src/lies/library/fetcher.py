"""Concrete ``Fetcher`` that drives an existing scraper + format dispatch.

Bridges the abstract ``Fetcher`` protocol (Task 8's
``lies.library.ingest.Fetcher``) to the existing ``lies.scrapers`` and
``lies.etl.normalize.format_dispatch`` modules. The library is a sibling
of any wiki's data root, so this fetcher deliberately avoids the obsidian
frontmatter pass that requires a wiki ``Collection`` -- it produces a
clean markdown body that the ingest pipeline can hand to the mirror
writer, which applies the deterministic frontmatter with the upstream
``source_hash`` flowing into the ``ingested_at`` derivation.

Bespoke scrapers (those configured via ``Collection.scraper_cmd``) are
loaded through the same ``module:attr`` / ``path.py:attr`` resolver the
wiki-side ``_load_bespoke_scraper`` uses. If the resolver fails, the
exception propagates -- the fetcher does NOT silently fall through to
``pick_scraper`` (that would cause bespoke collections to ingest
through the wrong scraper and lose data).

Per-doc flow:

1. ``pick_scraper(source)`` (or bespoke loader) selects a ``BaseScraper``
   subclass instance.
2. ``scraper.fetch(source)`` returns raw bytes.
3. ``scraper.parse(raw, source=source)`` returns a list of ``ParsedDoc``.
4. For each ``ParsedDoc``:
   - If ``source_format`` is in ``REGISTRY.formats()`` (and not
     ``markdown``), the bytes are materialized into a per-doc temp
     workspace and routed through the matching ``Builder`` -- mirroring
     ``etl/stages/normalize.py:83-90``.
   - Otherwise ``format_dispatch.dispatch`` produces a markdown body
     (markdown / html / rst / pdf formats handled; unknown formats raise
     ``UnknownFormatError`` to the caller for quarantine).
5. The upstream ``source_sha256`` (or ``sha256(content)`` when blank) is
   preserved on ``FetchItem.source_hash`` so the mirror writer can stamp
   it into the deterministic frontmatter.
6. Zero-emission scrapers raise ``LibraryFetchUnreachable`` at run
   boundary (Task 7's typed error), so the operator notices the empty
   batch instead of silently committing nothing.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from lies.library.errors import LibraryFetchUnreachable
from lies.library.ingest import FetchItem
from lies.scrapers import base as _scraper_base
from lies.scrapers.base import BaseScraper, ParsedDoc
from lies.scrapers.errors import ScraperUnavailable

if TYPE_CHECKING:
    from lies.collections.record import Collection


def _hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_bespoke_scraper(scraper_cmd: str) -> BaseScraper:
    """Resolve ``module:attr`` or ``path.py:attr`` to a BaseScraper.

    Mirrors ``etl/stages/scrape.py:_load_bespoke_scraper`` so the
    library-side fetcher honors ``Collection.scraper_cmd`` without a
    silent fall-through to ``pick_scraper``. On any failure
    (``ScraperUnavailable``) the exception propagates so the caller can
    surface the bespoke-loader failure to the operator.
    """
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


def _materialize(workspace: Path, fmt: str, raw: bytes) -> None:
    """Place ``raw`` at the path the builder expects for ``fmt``.

    Mirrors ``etl/stages/normalize.py:_materialize`` so REGISTRY routing
    can reuse the same on-disk shape (PDF reads ``source.pdf``; HTML
    reads ``source.html``; Sphinx walks ``src/``).
    """
    if fmt == "pdf":
        (workspace / "source.pdf").write_bytes(raw)
    elif fmt == "html":
        (workspace / "source.html").write_bytes(raw)
    elif fmt == "sphinx":
        (workspace / "src").mkdir(parents=True, exist_ok=True)
        (workspace / "src" / "index.rst").write_bytes(raw)
    else:
        # ``liquid`` reads ``source.liquid``/``source.html``; the builder
        # raises ``BuilderFetchFailed`` if neither is present.
        (workspace / f"source.{fmt}").write_bytes(raw)


def _materialize_bespoke(workspace: Path, doc: ParsedDoc) -> None:
    """Materialize a synthetic manifest + body file for a bespoke doc.

    Mirrors ``etl/stages/normalize.py:_materialize_bespoke`` so the
    bespoke builder sees the same on-disk layout it expects. The
    manifest points at the per-doc body; the builder reads
    ``<workspace>/manifest.json`` and ``<workspace>/<entry.path>``.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    body_name = doc.path.rsplit("/", 1)[-1] or "body.md"
    (workspace / body_name).write_bytes(doc.content)
    manifest = {
        "files": [
            {
                "path": body_name,
                "out_path": doc.path,
                "source_format": "markdown",
                "sha256": doc.source_sha256,
            }
        ]
    }
    (workspace / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _normalize_body(
    doc: ParsedDoc,
    *,
    collection: Collection | None = None,
) -> str:
    """Markdown body without wiki/collection context.

    Routing order (mirrors ``etl/stages/normalize.py:83-90``):

    1. If ``doc.source_format`` is in :data:`REGISTRY.formats()` (and
       not ``markdown``), the bytes are materialized into a per-doc
       temp workspace and the matching :class:`Builder` produces the
       markdown body. The fetcher has no wiki/obsidian context, so the
       builder's output is taken verbatim -- frontmatter is the library
       mirror writer's concern.
    2. Otherwise ``format_dispatch.dispatch`` produces the body
       (markdown / html / rst / pdf formats handled; unknown formats
       raise ``UnknownFormatError`` to the caller for quarantine).
    3. Errors propagate so a PDF or pandoc outage cannot silently land
       raw binary / raw HTML in a mirror file as if it were clean
       markdown; the ingest pipeline can then quarantine or surface
       the failure explicitly.
    """
    # Lazy imports keep ``import lies.cli`` from pulling the
    # builder package (and its transitive pydantic_ai / fastmcp
    # stack) into ``sys.modules``; only docs that actually route
    # through REGISTRY pay the load cost.
    from lies.builders.base import REGISTRY
    from lies.builders.errors import BuilderError

    if doc.source_format in REGISTRY.formats() and doc.source_format != "markdown":
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            if doc.source_format == "bespoke":
                _materialize_bespoke(workspace, doc)
            else:
                _materialize(workspace, doc.source_format, doc.content)
            built = REGISTRY.resolve(doc.source_format).build(
                workspace,
                collection=collection,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            )
        if not built:
            # Surface an explicit error rather than committing an empty
            # body; the caller will quarantine the doc with the reason.
            raise BuilderError(f"builder produced no docs for {doc.path}")
        return "\n\n".join(b.content.decode("utf-8", errors="replace") for b in built)

    from lies.etl.normalize.format_dispatch import dispatch

    return dispatch(doc.content, doc.source_format)


class ScraperFetcher:
    """Concrete ``Fetcher`` driving an existing scraper + format dispatch.

    Module-attribute lookup (``_scraper_base.pick_scraper``) instead of a
    ``from ... import`` binding keeps ``monkeypatch.setattr`` on
    ``lies.scrapers.base.pick_scraper`` effective in tests.

    ``scraper_cmd`` honors ``Collection.scraper_cmd``: when set, the
    bespoke scraper is loaded via :func:`_load_bespoke_scraper` instead
    of falling back to :func:`lies.scrapers.base.pick_scraper`. Any
    failure in the loader propagates -- the fetcher never silently
    fall-throughs to the URL/path heuristic.

    ``collection`` is threaded into REGISTRY builders that read
    ``Collection.config`` (sphinx includes/excludes/renames, liquid
    ``render_cmd``, etc.). Without it, builder-routed docs would crash;
    the fetcher passes it through verbatim.
    """

    def __init__(
        self,
        library: object | None,
        *,
        scraper_cmd: str | None = None,
        collection: Collection | None = None,
    ) -> None:
        self._library = library
        self._scraper_cmd = scraper_cmd
        self._collection = collection

    def fetch_sources(self, source: Path | str) -> Iterator[FetchItem]:
        if self._scraper_cmd is not None:
            scraper = _load_bespoke_scraper(self._scraper_cmd)
        else:
            scraper = _scraper_base.pick_scraper(source)
        raw = scraper.fetch(source)
        docs = scraper.parse(raw, source=source)
        emitted = 0
        for doc in docs:
            source_hash = doc.source_sha256 or _hash_bytes(doc.content)
            yield FetchItem(
                path=Path(doc.path),
                url=str(source) if not isinstance(source, Path) else None,
                body=_normalize_body(doc, collection=self._collection),
                source_hash=source_hash,
                fetched_via=type(scraper).__name__,
            )
            emitted += 1
        if emitted == 0:
            raise LibraryFetchUnreachable(f"scraper produced 0 items for {source}")


__all__ = ("ScraperFetcher",)
