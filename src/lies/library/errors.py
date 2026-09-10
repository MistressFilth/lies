"""Typed errors raised at library run boundaries (NOT per-doc)."""


class LibraryError(Exception):
    """Base for library-level errors that abort a run."""


class LibraryFetchUnreachable(LibraryError):
    """Zero documents survived fetch — entire run aborted."""


class LibraryCatalogLocked(LibraryError):
    """catalog.db busy_timeout (5000ms) exceeded; operator intervention required."""


class LibraryAtomicCommitFailed(LibraryError):
    """git commit failed after files staged; staged files preserved for forensics."""


__all__ = (
    "LibraryError",
    "LibraryFetchUnreachable",
    "LibraryCatalogLocked",
    "LibraryAtomicCommitFailed",
)
