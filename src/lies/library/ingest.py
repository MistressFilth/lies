"""Five-step deterministic ingest pipeline orchestrator.

The pipeline runs five phases per fetched document:

1. **fetch**     — ``Fetcher.fetch_sources(source)`` yields ``FetchItem``s
2. **ETL**       — concrete fetcher responsibility (Task 9's ``ScraperFetcher``)
3. **filter**    — filename gate (``should_skip_filename``) + content gate
                   (``should_skip_content``)
4. **mirror**    — ``write_mirror`` writes the deterministic frontmatter + body
5. **catalog**   — ``LibraryWriter.commit`` upserts the catalog page and commits
                   atomically

Per-doc quarantine: failed docs are copied to
``library.git_root/poison/<collection>/<slug>.md`` for inspection, recorded in
``BatchIngestResult.quarantine_records``.

Atomic-commit: the whole batch commits once at ``_finalize`` time (per the
``LibraryWriter`` envelope from Task 7), unless ``dry_run=True`` skips the
commit and the per-doc ``write_mirror`` entirely.
"""

from __future__ import annotations

from collections.abc import Container, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from lies.library.catalog import LibraryCatalogPage
from lies.library.errors import LibraryFetchUnreachable
from lies.library.filter import should_skip_content, should_skip_filename
from lies.library.mirror import write_mirror
from lies.library.paths import Library, LibraryCollection
from lies.library.slug import derive_slug, validate_slug
from lies.library.writer import LibraryWriter


@dataclass(frozen=True)
class FetchItem:
    """One document handed back by a ``Fetcher``.

    Either ``path`` (a local source file path) or ``url`` (a remote source
    URL) may be ``None``; at least one is expected. ``body`` is the rendered
    text the ETL stage produced. ``source_hash`` is the upstream content
    hash (used both for idempotency and for the deterministic ``ingested_at``
    derivation in frontmatter). ``fetched_via`` names the scraper that
    produced the item.
    """

    path: Path | None
    url: str | None
    body: str
    source_hash: str
    fetched_via: str


class Fetcher(Protocol):
    """Pluggable source-fetcher protocol.

    Real implementations (Task 9's ``ScraperFetcher``, future Web/PDF/GitHub
    variants) live in ``lies.scrapers`` and adapt ``fetch_sources`` against
    a downloaded source. The protocol is plumbed through so tests can inject
    ``_StaticFetcher`` without touching the network.
    """

    def fetch_sources(self, source: Path | str) -> Iterator[FetchItem]: ...


@dataclass(kw_only=True)
class BatchIngestResult:
    """Aggregate counters for one ``run_source_ingest`` / ``run_batch_ingest`` call.

    ``skip_reasons`` is keyed on the leading category (``skip-stem``,
    ``skip-stem-prefix``, ``skip-content``) so a single dict surfaces
    high-level skip mix. ``mirror_paths`` is the list of files actually
    written (empty under ``dry_run=True``). ``quarantine_records`` is the
    list of ``(relative_poison_path, reason)`` tuples for per-doc failures
    (empty under ``dry_run=True`` for mirror-collision only — filter-gate
    failures still record regardless of dry-run since they don't write).
    """

    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    mirror_paths: list[Path] = field(default_factory=list)
    quarantine_records: list[tuple[str, str]] = field(default_factory=list)


def _record_skip(result: BatchIngestResult, reason: str) -> None:
    """Increment skipped + skip_reasons keyed on the leading category."""
    key = reason.split(":", 1)[0]
    result.skip_reasons[key] = result.skip_reasons.get(key, 0) + 1
    result.skipped += 1


def _quarantine_to_poison(
    collection: LibraryCollection,
    slug: str,
    body: str,
    reason: str,
) -> tuple[str, str]:
    """Copy the body to ``poison/<collection>/<slug>.md`` and return the record.

    Per-doc quarantine: preserves the failed doc for inspection, mirroring
    the wiki-side ``lies.etl.quarantine.quarantine`` contract. Returns a
    ``(relative_path, reason)`` tuple suitable for
    ``BatchIngestResult.quarantine_records``.
    """
    target = collection.library.poison_root / collection.name / f"{slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return (str(target.relative_to(collection.library.git_root)), reason)


def _process_item(
    item: FetchItem,
    library: Library,
    collection_name: str,
    *,
    exclude_stems: Container[str] = (),
    exclude_dirs: Container[str] = (),
    force: bool,
    dry_run: bool,
    result: BatchIngestResult,
) -> None:
    """Run phases 3-5 for one fetched document.

    Returns ``None`` after appending to ``result``; the caller iterates over
    all items and then calls ``_finalize`` to commit the batch. Skips /
    quarantines / mirror-collisions all return early without raising.
    """
    coll = library.collection(collection_name)
    path = item.path
    if path is not None:
        skip_reason = should_skip_filename(
            path,
            extra_stems=exclude_stems,
            extra_prefixes=exclude_dirs,
        )
        if skip_reason:
            _record_skip(result, skip_reason)
            return
        slug = derive_slug(path)
    else:
        slug = item.url.rsplit("/", 1)[-1].rsplit("?", 1)[0] if item.url else "page"
        slug = slug.lower().replace("_", "-")
    try:
        validate_slug(slug)
    except ValueError:
        result.errors += 1
        result.quarantine_records.append(
            _quarantine_to_poison(coll, slug, item.body, "invalid-slug")
        )
        return

    skip_reason = should_skip_content(item.body)
    if skip_reason:
        result.errors += 1
        result.quarantine_records.append(_quarantine_to_poison(coll, slug, item.body, skip_reason))
        return

    source_url = item.url
    source_path = str(path) if path is not None else None
    target = coll.dir / f"{slug}.md"

    if target.exists() and not force:
        result.errors += 1
        result.quarantine_records.append(
            _quarantine_to_poison(
                coll,
                slug,
                item.body,
                f"mirror-collision:{slug}:{item.source_hash[:8]}",
            )
        )
        return

    if dry_run:
        return

    written = write_mirror(
        coll,
        slug=slug,
        body=item.body,
        source_url=source_url,
        source_path=source_path,
        source_hash=item.source_hash,
        fetched_via=item.fetched_via,
        force=force,
    )
    if target.exists() and not (target == written and target.stat().st_size > 0):
        result.updated += 1
    else:
        result.created += 1
    result.mirror_paths.append(written)


def _finalize(
    library: Library,
    collection_name: str,
    result: BatchIngestResult,
    *,
    dry_run: bool,
    message: str,
) -> BatchIngestResult:
    """Commit the batch atomically (Task 7's ``LibraryWriter`` envelope).

    No-op when ``dry_run=True`` (no commit, no catalog upsert) or when no
    files were written (``mirror_paths`` empty — likely the fetcher yielded
    nothing useful and the run will already have raised upstream via
    ``LibraryFetchUnreachable``). Catalog upserts are best-effort: failures
    raise through ``LibraryWriter`` (preserving ``LibraryAtomicCommitFailed`` /
    ``LibraryCatalogLocked`` semantics).
    """
    if dry_run or not result.mirror_paths:
        return result
    writer = LibraryWriter(library)
    rel_paths = [p.relative_to(library.git_root) for p in result.mirror_paths]
    updated_iso = datetime.now(UTC).isoformat()
    catalog_updates = [
        LibraryCatalogPage(
            slug=f"{collection_name}/{p.stem}",
            title=p.stem.replace("-", " ").title(),
            type="",
            source_pkg=collection_name,
            section="library",
            updated=updated_iso,
            hash="",
            derived_from="",
        )
        for p in result.mirror_paths
    ]
    sha = writer.commit(
        rel_paths,
        message=message,
        catalog_updates=catalog_updates,
    )
    if sha is None:
        # Empty rel_paths + non-empty catalog_updates case: ``LibraryWriter``
        # short-circuits to ``None`` and skips the catalog upsert. Mirror
        # PR #38 contract — accept and continue.
        pass
    return result


def run_source_ingest(
    library: Library,
    collection_name: str,
    source: Path | str,
    *,
    fetcher: Fetcher,
    slug: str | None = None,
    title: str | None = None,
    exclude_stems: Container[str] = (),
    exclude_dirs: Container[str] = (),
    force: bool = False,
    dry_run: bool = False,
) -> BatchIngestResult:
    """Ingest a single source (URL or path).

    Raises ``LibraryFetchUnreachable`` when the fetcher yields zero items —
    the entire run aborts so the operator notices (no silent empty batch).
    """
    result = BatchIngestResult()
    items = list(fetcher.fetch_sources(source))
    if not items:
        raise LibraryFetchUnreachable(f"no items fetched from {source}")
    for item in items:
        _process_item(
            item,
            library,
            collection_name,
            exclude_stems=exclude_stems,
            exclude_dirs=exclude_dirs,
            force=force,
            dry_run=dry_run,
            result=result,
        )
    return _finalize(
        library,
        collection_name,
        result,
        dry_run=dry_run,
        message=f"ingest: {collection_name} +{result.created}",
    )


def run_batch_ingest(
    library: Library,
    collection_name: str,
    source_dir: Path,
    *,
    fetcher: Fetcher,
    exclude_stems: Container[str] = (),
    exclude_dirs: Container[str] = (),
    force: bool = False,
    dry_run: bool = False,
) -> BatchIngestResult:
    """Ingest every eligible file under ``source_dir``.

    Directory walk is the fetcher's responsibility — ``run_batch_ingest``
    just delegates to ``Fetcher.fetch_sources(source_dir)`` and processes
    the yielded items through the same pipeline as ``run_source_ingest``.
    """
    result = BatchIngestResult()
    items = list(fetcher.fetch_sources(source_dir))
    if not items:
        raise LibraryFetchUnreachable(f"no items fetched from {source_dir}")
    for item in items:
        _process_item(
            item,
            library,
            collection_name,
            exclude_stems=exclude_stems,
            exclude_dirs=exclude_dirs,
            force=force,
            dry_run=dry_run,
            result=result,
        )
    return _finalize(
        library,
        collection_name,
        result,
        dry_run=dry_run,
        message=f"ingest: {collection_name} +{result.created}",
    )


__all__ = (
    "BatchIngestResult",
    "FetchItem",
    "Fetcher",
    "run_source_ingest",
    "run_batch_ingest",
)
