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
    compatible with :class:`lies.collections.record.Collection` for
    the two fields the resolver reads (``name``, ``tags``), so the
    retriever's matching code passes it through without a shim.

  - :func:`library_collection_names` — sorted tuple of every
    addressable collection directory name. The resolver validates
    each ``Include`` atom against this set.

  - :func:`library_initialized` — whether the library's
    ``collections_root`` exists on disk. The error surface names the
    gap when False.

  - :func:`library_has_no_collections` — whether the library is
    initialized but has zero collections (the empty-but-present case).
    Lets the error surface tell the operator to ingest something
    rather than guess at tag spellings.

All four primitives read the live filesystem; nothing is cached
beyond :class:`Library.open`'s singleton — re-reading on every call
is cheap (one ``iterdir`` over a few directories) and avoids
stale-cache traps when the operator adds a new collection.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from lies.library.paths import Library


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
    collection, so :class:`lies.collections.record.Collection` and
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


def library_collection_names() -> frozenset[str]:
    """Sorted tuple of every addressable library-collection directory name.

    The addressable-tag set for the resolver. Empty when the library
    is uninitialized or empty. Sorted for deterministic error messages.
    """
    root = _collections_root()
    if not root.exists():
        return frozenset()
    return frozenset(sorted(entry.name for entry in root.iterdir() if entry.is_dir()))


def library_collection_metas() -> Iterator[LibraryCollectionMeta]:
    """Yield a :class:`LibraryCollectionMeta` per directory in the library.

    Used by the retriever's :func:`_collections_matching`. The
    resolver's matching code is structurally compatible with both
    :class:`lies.collections.record.Collection` (legacy wiki-yaml
    shape) and :class:`LibraryCollectionMeta` (library-first shape);
    the library model is the canonical source going forward.
    """
    root = _collections_root()
    if not root.exists():
        return
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            yield LibraryCollectionMeta(name=entry.name)


__all__ = (
    "LibraryCollectionMeta",
    "library_collection_metas",
    "library_collection_names",
    "library_has_no_collections",
    "library_initialized",
)
