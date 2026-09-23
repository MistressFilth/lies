"""Library-collection registry — the single source of truth for tag resolution.

Library collections live as directories under
``Library.collections_root/<name>/``; they are canonical source docs,
not wikis, and carry no per-collection yaml config. The directory
name is the only metadata the addressable-tag surface needs.

This module is the home for the four primitives every tag-resolution
call site (CLI ``query`` / MCP ``query`` and ``answer`` / retriever's
``_collections_matching`` / orchestrator's ``_searched_scope``) needs:

  - :class:`LibraryCollectionMeta` — slim record carrying only ``name``
    and ``tags`` (always empty under the library layout, but the
    ``tags`` field is retained for forward compat with a future
    per-collection metadata sidecar). The dataclass is structurally
    compatible with the legacy wiki-yaml Collection shape for the two
    fields the resolver reads (``name``, ``tags``), so the retriever's
    matching code passes it through without a shim.

  - :func:`library_collection_names` — sorted tuple of every
    addressable collection directory name. The resolver validates
    each ``Include`` atom against this set. Memoized via
    :func:`functools.lru_cache` so the per-call ``iterdir`` only runs
    once per process; tools whose hot path calls this surface
    (notably the F19 ``ground`` tool) avoid redundant disk reads.
    Cache invalidates on process restart; the registry is small
    enough that this is acceptable for long-running daemons.

  - :func:`library_collection_tags` — sorted frozenset of every
    ``tags`` entry across all registered collections'
    ``config.yaml``. The MCP ``query`` / ``answer`` validator
    unions this with ``c:<name>`` so a ``t:<tag>`` filter against
    a library-collection tag does not raise ``TagExprUnknown``.
    Memoized to match :func:`library_collection_names`.

  - :func:`library_git_root` — the library's git root. Library
    collections are registered into qmd at this path, so the
    dual-source librarian fan-out queries ``qmd_query`` against this
    cwd for the library side (and against ``wiki.wiki_dir`` for the
    wiki side).

  - :func:`library_initialized` — whether the library's
    ``collections_root`` exists on disk. The error surface names the
    gap when False.

  - :func:`library_has_no_collections` — whether the library is
    initialized but has zero collections (the empty-but-present case).
    Lets the error surface tell the operator to ingest something
    rather than guess at tag spellings.

All four primitives read the live filesystem at least once;
:func:`library_collection_names` memoizes its result so the
per-call disk walk runs only on the first invocation.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from lies.library.config_io import config_path_for
from lies.library.errors import CollectionConfigInvalid, CollectionNotFound
from lies.library.paths import Library
from lies.library.record import LibraryCollectionConfig


@dataclass(frozen=True)
class LibraryCollectionMeta:
    """Slim per-collection record for library-first tag resolution.

    Library collections carry no yaml config (no ``source``,
    ``scraper_cmd``, ``mapper_model``, ``language``, ``version``,
    etc.) — they are canonical source docs, not wikis. The resolver
    only needs the collection's name and (potentially) its declared
    tags, so this record holds exactly those two fields.

    The structural shape (``name: str``, ``tags: Sequence[str]``)
    matches what :func:`lies.query.tag_expr.atom_matches` and
    :func:`lies.query.tag_expr._exclude_atom_matches` read off a
    collection, so the legacy wiki-yaml Collection and
    :class:`LibraryCollectionMeta` are interchangeable at the
    resolver boundary. This keeps the library-first migration
    additive — no rewrites to the matching code — while preventing
    the library model from re-introducing wiki-yaml metadata.

    ``tags`` is always ``()`` under the current library layout. The
    field exists so a future per-collection metadata sidecar (e.g.
    ``<coll>/.lies/manifest.json`` with declared tags) can populate
    it without a resolver signature change.
    """

    name: str
    tags: tuple[str, ...] = ()


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
def library_collection_names() -> frozenset[str]:
    """Sorted frozenset of every addressable library-collection directory name.

    The addressable-tag set for the resolver. Empty when the library
    is uninitialized or empty. Sorted for deterministic error messages.

    Memoized via :func:`functools.lru_cache`: the first call walks
    the library's ``collections_root`` once and returns a frozen
    snapshot; subsequent calls return the cached snapshot without
    re-traversing the disk. Cache invalidates on process restart —
    acceptable because the registry is small and the hot paths
    (F15 tag-filter dispatch, F19 ground tool, CLI query surface)
    would otherwise re-do an ``iterdir`` on every invocation. The
    test surface can clear the cache via
    ``library_collection_names.cache_clear()``.
    """
    root = _collections_root()
    if not root.exists():
        return frozenset()
    return frozenset(sorted(entry.name for entry in root.iterdir() if entry.is_dir()))


def library_collection_metas() -> Iterator[LibraryCollectionMeta]:
    """Yield a :class:`LibraryCollectionMeta` per directory in the library.

    Used by the retriever's :func:`_collections_matching`. The
    resolver's matching code is structurally compatible with both the
    legacy wiki-yaml Collection shape and :class:`LibraryCollectionMeta`
    (library-first shape); the library model is the canonical source
    going forward.

    Reads the per-collection ``config.yaml`` sidecar so the resolver
    sees declared ``tags`` (the ``t:`` / ``c:`` qualifier dispatch
    in :mod:`lies.query.tag_expr` keys off tags ∪ {name}). When the
    config is missing or malformed, falls back to a no-tags meta so
    the resolver still surfaces the collection's name (the implicit
    self-tag rule).
    """
    root = _collections_root()
    if not root.exists():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        config = entry / "config.yaml"
        tags: tuple[str, ...] = ()
        if config.exists():
            try:
                from lies.library.config_io import load_config

                record = load_config(entry.name)
                if record is not None and record.tags:
                    tags = tuple(record.tags)
            except Exception:
                tags = ()
        yield LibraryCollectionMeta(name=entry.name, tags=tags)


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
def library_collection_tags() -> frozenset[str]:
    """Sorted frozenset of every ``tags`` entry across all registered library
    collections' ``config.yaml``.

    Union of :attr:`LibraryCollectionConfig.tags` across all registered
    collections, surfaced via :func:`library_collection_records`. Empty
    when the library is uninitialized or has no collection tags. Sorted
    for deterministic error messages.

    Memoized via :func:`functools.lru_cache` to match
    :func:`library_collection_names`: the first call walks every
    collection's ``config.yaml`` once and returns a frozen snapshot;
    subsequent calls return the cached snapshot without re-traversing
    the disk. The test surface can clear the cache via
    ``library_collection_tags.cache_clear()``.
    """
    tags: set[str] = set()
    for record in library_collection_records():
        tags.update(record.tags)
    return frozenset(sorted(tags))


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
