"""Library-collection registry — the single source of truth for tag resolution."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from lies.library.config_io import config_path_for
from lies.library.errors import CollectionConfigInvalid, CollectionNotFound
from lies.library.paths import Library
from lies.library.record import LibraryCollectionConfig


@dataclass(frozen=True)
class LibraryCollectionMeta:
    """Slim per-collection record for library-first tag resolution."""

    name: str
    source_url: str = ""
    tags: frozenset[str] = field(default_factory=frozenset)
    scope_keywords: frozenset[str] = field(default_factory=frozenset)


def _collections_root() -> Path:
    """The library's ``collections_root`` directory.

    Thin wrapper over :meth:`Library.open` so callers don't have to
    import the ``Library`` class — the registry module is the single
    import surface for tag resolution.
    """
    return Library.open().collections_root


def library_git_root() -> Path:
    """The library's git root directory.

    Library collections are registered into qmd at this path, not at
    any wiki's ``wiki_dir``. The dual-source librarian fan-out calls
    ``qmd_query`` against this cwd for the library side and against
    ``wiki.wiki_dir`` for the wiki side. The two surfaces are
    distinct qmd indexes; collapsing them to a single cwd returns
    zero library hits even when the library is populated.

    Thin wrapper over :meth:`Library.open` so the registry module
    stays the single import surface for downstream callers (notably
    the librarian subagent's ``_wiki_search`` closure).
    """
    return Library.open().git_root


def library_initialized() -> bool:
    """Whether the library's ``collections_root`` exists on disk."""
    return _collections_root().exists()


def library_has_no_collections() -> bool:
    """True iff the library is initialized but has zero collections.

    Distinct from :func:`library_initialized` returning False (the
    library doesn't exist at all). The two cases get distinct error
    messages on the MCP / CLI surface so the operator knows whether
    to initialize the library or to ingest something into it.
    """
    root = _collections_root()
    if not root.exists():
        return False
    return not any(entry.is_dir() for entry in root.iterdir())


@lru_cache(maxsize=1)
def _library_collection_names_cached(_root_mtime_ns: int) -> frozenset[str]:
    """Cached snapshot of library-collection directory names.

    Cache key is the parent directory's mtime in nanoseconds —
    changing when a collection is added, removed, or renamed. The
    mtime-based key makes the cache self-invalidating for the
    long-running MCP daemon (a new collection like ``switchyard``
    was previously invisible until the daemon restarted). The
    directory walk only re-runs on cache miss; an unchanged dir
    keeps its hot-path speedup. Tests can still clear the cache
    via :func:`library_collection_names.cache_clear` (clears the
    outer wrapper) or by touching ``collections_root``.
    """
    root = _collections_root()
    if not root.exists():
        return frozenset()
    return frozenset(sorted(entry.name for entry in root.iterdir() if entry.is_dir()))


def library_collection_names() -> frozenset[str]:
    """Sorted frozenset of every addressable library-collection directory name."""
    root = _collections_root()
    if not root.exists():
        return frozenset()
    try:
        mtime_ns = root.stat().st_mtime_ns
    except OSError:
        # ``collections_root`` disappeared between ``exists()`` and
        # ``stat()`` — treat as empty rather than crash. Next call
        # will re-stat and re-walk.
        return frozenset()
    return _library_collection_names_cached(mtime_ns)


def library_collection_metas() -> Iterator[LibraryCollectionMeta]:
    """Yield a :class:`LibraryCollectionMeta` per directory in the library."""
    root = _collections_root()
    if not root.exists():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        config = entry / "config.yaml"
        tags: frozenset[str] = frozenset()
        scope_keywords: frozenset[str] = frozenset()
        source_url = ""
        if config.exists():
            try:
                from lies.library.config_io import load_config

                record = load_config(entry.name)
                if record is not None:
                    tags = frozenset(record.tags)
                    scope_keywords = frozenset(record.scope_keywords)
                    source_url = record.source
            except Exception:
                pass
        yield LibraryCollectionMeta(
            name=entry.name,
            source_url=source_url,
            tags=tags,
            scope_keywords=scope_keywords,
        )


def library_collection_records() -> Iterator[LibraryCollectionConfig]:
    """Yield a :class:`LibraryCollectionConfig` per collection with a valid config.

    Skips directories without ``config.yaml`` (e.g. fresh ingests that
    haven't been bootstrapped yet). Skips collections whose config
    fails validation; logs a warning per skip.
    """
    import logging

    from lies.library.config_io import load_config

    log = logging.getLogger(__name__)
    root = _collections_root()
    if not root.exists():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not config_path_for(entry.name).exists():
            continue
        try:
            yield load_config(entry.name)
        except (CollectionConfigInvalid, CollectionNotFound) as exc:
            log.warning("skipping malformed collection %s: %s", entry.name, exc)


def library_collection_record(slug: str) -> LibraryCollectionConfig | None:
    """Return the config record for ``slug`` if it exists, else ``None``."""
    from lies.library.config_io import load_config

    try:
        return load_config(slug)
    except CollectionNotFound:
        return None


@lru_cache(maxsize=1)
def _library_collection_tags_cached(_aggregate_mtime_ns: int) -> frozenset[str]:
    """Cached snapshot of the union of every collection's ``tags``.

    Cache key is the aggregate mtime of the ``collections_root``
    directory and every ``config.yaml`` it contains; the key changes
    when a collection is added, removed, renamed, or has its tags
    list edited. The aggregate-mtime key makes the cache
    self-invalidating for the long-running MCP daemon — the same
    fix applied to :func:`library_collection_names`. The full tag
    walk only runs on cache miss.
    """
    tags: set[str] = set()
    for record in library_collection_records():
        tags.update(record.tags)
    return frozenset(sorted(tags))


def library_collection_tags() -> frozenset[str]:
    """Sorted frozenset of every ``tags`` entry across all registered library collections'
    ``config.yaml``.
    """
    root = _collections_root()
    if not root.exists():
        return frozenset()
    aggregate_ns = _aggregate_mtime_ns(root)
    return _library_collection_tags_cached(aggregate_ns)


def _aggregate_mtime_ns(root: Path) -> int:
    """Return the maximum mtime in nanoseconds across ``root`` and its
    immediate ``config.yaml`` children.

    Used as a cache key for the per-collection tag union: any
    add/remove/rename of a collection, or any edit to a collection's
    ``config.yaml`` tags, bumps the aggregate and self-invalidates
    the cache. Skips a missing ``config.yaml`` (collection not yet
    bootstrapped) without raising.
    """
    try:
        root_mtime = root.stat().st_mtime_ns
    except OSError:
        return 0
    max_ns = root_mtime
    try:
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            config = entry / "config.yaml"
            if not config.exists():
                continue
            try:
                max_ns = max(max_ns, config.stat().st_mtime_ns)
            except OSError:
                continue
    except OSError:
        pass
    return max_ns


__all__ = (
    "LibraryCollectionMeta",
    "library_collection_metas",
    "library_collection_names",
    "library_collection_record",
    "library_collection_records",
    "library_collection_tags",
    "library_git_root",
    "library_has_no_collections",
    "library_initialized",
)
