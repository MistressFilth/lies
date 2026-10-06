"""Typed errors raised at library run boundaries (NOT per-doc)."""

from __future__ import annotations

from pathlib import Path


class LibraryError(Exception):
    """Base for library-level errors that abort a run."""


class LibraryFetchUnreachable(LibraryError):
    """Zero documents survived fetch — entire run aborted."""


class LibraryCatalogLocked(LibraryError):
    """catalog.db busy_timeout (5000ms) exceeded; operator intervention required."""


class LibraryAtomicCommitFailed(LibraryError):
    """git commit failed after files staged; staged files preserved for forensics."""


class SelfIngestRefused(LibraryError):
    """The source and the mirror destination are the same path.

    Raised when ``ingest`` is pointed at a collection directory (or at
    a page inside one). A run of that shape enumerates the mirrors and
    asks each one whether its own source has changed, which is a
    question with no non-circular answer: the only reachable outcomes
    are "skip everything" and "rewrite everything". Before this was
    refused it was neither — every page quarantined, nothing was
    written, and the reason named a hash conflict that had not
    happened.

    Refused at the run boundary rather than per page, because the fault
    is the caller's and one refusal names it once, instead of a poison
    copy and a ``.reason`` sidecar per page.
    """

    def __init__(self, source: Path, collection: str) -> None:
        self.source = source
        self.collection = collection
        super().__init__(
            f"refusing to ingest {source} into collection {collection!r}: the source "
            f"is the collection's own directory, so every page would be both the "
            f"source and the mirror. Ingest the upstream source instead, or name a "
            f"different --collection."
        )


"""Library-collection config error types (replaces the legacy wiki-yaml collection error module)."""


class CollectionError(Exception):
    """Base class for collection-config errors."""


class CollectionNotFound(CollectionError):
    """Requested collection does not exist."""


class CollectionConfigInvalid(CollectionError):
    """Collection config is malformed or fails validation."""


class CollectionNameRejected(CollectionConfigInvalid):
    """Collection name contains reserved characters."""

    def __init__(self, name: str) -> None:
        super().__init__(f"collection name contains reserved characters: {name!r}")
        self.name = name
        self.path = name


class CollectionWriteFailed(CollectionError):
    """Atomic write to a config YAML failed."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"failed to write {path}: {message}")


class CollectionAlreadyExists(CollectionError):
    """Target config already exists with different content."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class CollectionMismatch(CollectionError):
    """Existing config has a different source than requested."""

    def __init__(
        self,
        existing_source: str,
        requested_source: str,
    ) -> None:
        super().__init__(
            f"collection source mismatch: existing={existing_source!r}, "
            f"requested={requested_source!r}"
        )
        self.existing_source = existing_source
        self.requested_source = requested_source


class WizardAborted(CollectionError):
    """Wizard mode was declined."""

    def __init__(self) -> None:
        super().__init__("wizard cancelled; no collection written")


class WizardRequiresTTY(CollectionError):
    """--wizard requires an interactive TTY."""

    def __init__(self) -> None:
        super().__init__(
            "--wizard needs a TTY; run interactively or omit --wizard for bare scaffold"
        )


class WikiLayoutInitFailed(LibraryError):
    """Wiki layout bootstrap raised during auto-init."""

    def __init__(self, wiki_name: str, cause: BaseException) -> None:
        super().__init__(f"failed to auto-init wiki {wiki_name!r}: {cause}")
        self.wiki_name = wiki_name
        self.__cause__ = cause


__all__ = (
    "CollectionAlreadyExists",
    "CollectionConfigInvalid",
    "CollectionError",
    "CollectionMismatch",
    "CollectionNameRejected",
    "CollectionNotFound",
    "CollectionWriteFailed",
    "LibraryAtomicCommitFailed",
    "LibraryCatalogLocked",
    "LibraryError",
    "LibraryFetchUnreachable",
    "SelfIngestRefused",
    "WikiLayoutInitFailed",
    "WizardAborted",
    "WizardRequiresTTY",
)
