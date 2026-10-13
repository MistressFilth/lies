"""Concrete ``Fetcher`` that drives an existing scraper + format dispatch."""

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
from lies.builders.errors import BuilderParseError
from lies.scrapers.base import BaseScraper, ParsedDoc
from lies.scrapers.errors import ScraperUnavailable

if TYPE_CHECKING:
    from lies.library.record import LibraryCollectionConfig as Collection


def _hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# Short-ID mapping for ``fetched_via``. A bare ``type(scraper).__name__`` leaks the
# class hierarchy into mirror frontmatter and breaks the spec's short-id contract
# (``web`` / ``github`` / ``pdf``). An unmapped class falls back to its bare name, so
# a scraper added later is recorded rather than crashing the ingest.
_FETCHED_VIA_ID: dict[type[BaseScraper], str] = {}


def _register_short_id(cls: type[BaseScraper], short_id: str) -> None:
    """Register a concrete scraper class with a short ``fetched_via`` id.

    Idempotent: re-registering the same mapping is a no-op (we don't
    overwrite an existing entry to avoid surprises on import order).
    """
    _FETCHED_VIA_ID.setdefault(cls, short_id)


# Concrete registrations. Each is a one-line pin against the class
# name changing in the future — the cross-task contract is the short
# id, not the bare class name.
def _register_known_scrapers() -> None:
    """Populate ``_FETCHED_VIA_ID`` with the concrete scraper classes.

    Lazy import keeps ``import lies.cli`` cheap: the web/github/pdf
    scraper modules pull pydantic_ai / fastmcp transitively through the
    builder package. We only need their class objects here, which is
    what the registry pins anyway.
    """
    # Local imports mirror the lazy pattern in ``pick_scraper`` —
    # same import-graph reasoning applies.
    from lies.scrapers.github import GitHubScraper
    from lies.scrapers.pdf import PDFScraper
    from lies.scrapers.web import WebScraper

    _register_short_id(WebScraper, "web")
    _register_short_id(GitHubScraper, "github")
    _register_short_id(PDFScraper, "pdf")


def _short_id_for(scraper: BaseScraper) -> str:
    """Resolve the short id for ``scraper``.

    Falls back to ``type(scraper).__name__`` when the concrete class
    is not registered (e.g. a user-supplied bespoke scraper). The
    fallback preserves a non-empty ``fetched_via`` value rather than
    silently dropping the field — the operator still gets a marker
    they can grep for.
    """
    if not _FETCHED_VIA_ID:
        _register_known_scrapers()
    return _FETCHED_VIA_ID.get(type(scraper), type(scraper).__name__)


def _load_bespoke_scraper(scraper_cmd: str) -> BaseScraper:
    """Resolve ``module:attr`` or ``path.py:attr`` to a BaseScraper.

    Mirrors ``etl/stages/scrape.py:_load_bespoke_scraper`` so the
    library-side fetcher honors ``Collection.scraper_cmd`` without a
    silent fall-through to ``pick_scraper``. On any failure
    (``ScraperUnavailable``) the exception propagates so the caller can
    surface the bespoke-loader failure to the operator. ``fetch_sources``
    wraps the ``ScraperUnavailable`` in ``LibraryFetchUnreachable`` for
    taxonomy consistency (Minor 44).
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
    """Place ``raw`` at the path the builder expects for ``fmt``."""
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

    Minor 45: same rationale as :func:`_materialize` — verbatim mirror
    of the canonical implementation in ``etl/stages/normalize.py``.
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
    """Markdown body without wiki/collection context."""
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
            # A builder reading ``collection.config`` without a real ``Collection`` raises
            # ``AttributeError`` on the bare ``None``, which would mask as a per-doc quarantine
            # entry. Re-raise as ``BuilderError`` so the operator sees "collection required"
            # rather than a confusing ``'NoneType' object has no attribute 'config'``.
            try:
                built = REGISTRY.resolve(doc.source_format).build(
                    workspace,
                    collection=collection,  # ty: ignore[invalid-argument-type]
                )
            except AttributeError as exc:
                raise BuilderError(
                    f"builder for {doc.source_format!r} requires a Collection "
                    f"(got {type(collection).__name__}): {exc}"
                ) from exc
        if not built:
            # Surface an explicit error rather than committing an empty
            # body; the caller will quarantine the doc with the reason.
            raise BuilderError(f"builder produced no docs for {doc.path}")
        return "\n\n".join(b.content.decode("utf-8", errors="replace") for b in built)

    from lies.etl.normalize.format_dispatch import dispatch

    return dispatch(doc.content, doc.source_format)


# What the directory walk admits: text documents, plus anything the builder REGISTRY
# can render (routed through :func:`_normalize_body`, the ``--source`` path).
# ``should_skip_filename`` is a *denylist* and cannot express "is a document" — a
# ``config.yaml`` in a collection directory became a ``config.md`` page, 36 times over.
_TEXT_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".rst", ".txt"})

# Extension -> ``source_format`` for the formats a Builder can render. Kept
# as an explicit map rather than derived from ``REGISTRY.formats()`` because
# the registry is keyed by format name, not by file extension: ``liquid`` and
# ``sphinx`` name build inputs, not suffixes, and ``bespoke`` names none.
_REGISTRY_SUFFIX_FORMATS: dict[str, str] = {".pdf": "pdf", ".html": "html", ".htm": "html"}


class ScraperFetcher:
    """Concrete ``Fetcher`` driving an existing scraper + format dispatch."""

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
        # ``--batch <DIR>`` walks at depth 1, one ``FetchItem`` per regular file, so the
        # dispatcher stays URL/file-shaped. Subdirectories are skipped (the contract is a flat
        # walk, and nested files collide on the slug); unreadable files drop silently,
        # mirroring ``WebScraper._parse_index``.
        if isinstance(source, Path) and source.is_dir():
            yield from self._iter_directory(source)
            return

        if self._scraper_cmd is not None:
            # Wrap a bespoke loader's ``ScraperUnavailable`` into ``LibraryFetchUnreachable`` at
            # the call site, not inside ``_load_bespoke_scraper`` — so it also fires when a test
            # monkeypatches the loader with a stub that raises directly, the one case the
            # production function cannot see.
            try:
                scraper = _load_bespoke_scraper(self._scraper_cmd)
            except ScraperUnavailable as exc:
                raise LibraryFetchUnreachable(
                    f"bespoke scraper loader failed for {self._scraper_cmd!r}: {exc}"
                ) from exc
        else:
            scraper = _scraper_base.pick_scraper(source)
        raw = scraper.fetch(source)
        # Hash the raw bytes ONCE up front, so the same source URL yields the same
        # ``source_hash`` whichever scraper's per-doc parser handles it. ``ingested_at``
        # derives from ``source_hash``, so per-doc slices would break idempotency.
        fetcher_raw_hash = _hash_bytes(raw)
        docs = scraper.parse(raw, source=source)
        emitted = 0
        for doc in docs:
            yield FetchItem(
                path=Path(doc.path),
                url=str(source) if not isinstance(source, Path) else None,
                body=_normalize_body(doc, collection=self._collection),
                source_hash=fetcher_raw_hash,
                # Short-ID mapping; the bare ``type(scraper).__name__`` leaks the class hierarchy.
                # ``_FETCHED_VIA_ID`` is populated lazily by ``_register_known_scrapers`` on first use.
                fetched_via=_short_id_for(scraper),
            )
            emitted += 1
        if emitted == 0:
            raise LibraryFetchUnreachable(f"scraper produced 0 items for {source}")

    def _iter_directory(self, directory: Path) -> Iterator[FetchItem]:
        """Recursive walk of ``directory``; yield one ``FetchItem`` per file."""
        base = directory.resolve()

        def _walk(rel: Path) -> Iterator[FetchItem]:
            for entry in sorted(rel.iterdir()):
                if entry.is_dir():
                    yield from _walk(entry)
                    continue
                if not entry.is_file():
                    continue
                suffix = entry.suffix.lower()
                source_format = _REGISTRY_SUFFIX_FORMATS.get(suffix)
                if source_format is None and suffix not in _TEXT_SUFFIXES:
                    continue
                try:
                    body_bytes = entry.read_bytes()
                except OSError:
                    continue
                raw_hash = hashlib.sha256(body_bytes).hexdigest()
                if source_format is None:
                    body = body_bytes.decode("utf-8", errors="replace")
                else:
                    # Same route as ``--source``: materialize into a temp
                    # workspace and let the Builder render it to markdown, so
                    # a PDF becomes its extracted text and ``source_hash``
                    # stays the hash of the *raw bytes* per the spec.
                    try:
                        body = _normalize_body(
                            ParsedDoc(
                                path=entry.name,
                                content=body_bytes,
                                source_sha256=raw_hash,
                                source_format=source_format,
                            ),
                            collection=self._collection,
                        )
                    except BuilderParseError:
                        # A file that will not render is dropped, matching the
                        # walker's stance on unreadable files -- raising here
                        # would abort the whole batch.
                        continue
                yield FetchItem(
                    path=entry.relative_to(base),
                    url=None,
                    body=body,
                    source_hash=raw_hash,
                    fetched_via="local",
                )

        yield from _walk(base)


__all__ = ("ScraperFetcher",)
