"""Library: deterministic ingest destination, sibling of wiki."""

from lies.library.errors import (
    LibraryAtomicCommitFailed,
    LibraryCatalogLocked,
    LibraryError,
    LibraryFetchUnreachable,
)
from lies.library.paths import Library, LibraryCollection
from lies.library.writer import LibraryWriter

__all__ = (
    "Library",
    "LibraryCollection",
    "LibraryWriter",
    "LibraryError",
    "LibraryFetchUnreachable",
    "LibraryCatalogLocked",
    "LibraryAtomicCommitFailed",
)
