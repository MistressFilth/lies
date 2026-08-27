"""Typed exceptions for the collections package."""

from __future__ import annotations

from pathlib import Path


class CollectionError(Exception):
    """Base class for collection-level errors."""


class CollectionNotFound(CollectionError):
    """Requested collection does not exist in the wiki."""


class CollectionConfigInvalid(CollectionError):
    """Collection config is malformed or fails validation."""


class CollectionNameRejected(CollectionConfigInvalid):
    """Collection name contains reserved operator characters."""

    def __init__(self, name: str) -> None:
        super().__init__(f"collection name contains reserved characters: {name!r}")
        self.name = name
        self.path = name


class RegistryCorrupt(Exception):
    """A registry file failed JSON parsing or schema validation."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"registry at {path} is corrupt: {reason}")
        self.path = path
        self.reason = reason


class RegistryVersionUnsupported(Exception):
    """The registry file's ``version`` field is not supported by this loader."""

    def __init__(self, path: Path, found: int, supported: int) -> None:
        super().__init__(
            f"registry at {path} has version {found}, this build supports up to version {supported}"
        )
        self.path = path
        self.found = found
        self.supported = supported


class RegistryWriteFailed(Exception):
    """Atomic rename of the registry temp file failed."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"failed to write registry at {path}: {reason}")
        self.path = path
        self.reason = reason


class CollectionWriteFailed(Exception):
    """Raised when an atomic write to a collection YAML config fails."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"failed to write {path}: {message}")


class CollectionMismatch(CollectionError):
    """Existing collection YAML has a different source than the CLI requested."""

    def __init__(
        self,
        existing_source: str,
        existing_format: str | None,
        requested_source: str,
        requested_format: str | None,
    ) -> None:
        super().__init__(
            f"collection source mismatch: existing={existing_source!r} "
            f"(format={existing_format!r}), requested={requested_source!r} "
            f"(format={requested_format!r})"
        )
        self.existing_source = existing_source
        self.existing_format = existing_format
        self.requested_source = requested_source
        self.requested_format = requested_format


class WizardAborted(CollectionError):
    """Wizard mode (--wizard) was declined by the user or agent."""

    def __init__(self) -> None:
        super().__init__("wizard cancelled; no collection written")


class WizardRequiresTTY(CollectionError):
    """--wizard requires an interactive TTY."""

    def __init__(self) -> None:
        super().__init__(
            "--wizard needs a TTY; run interactively or omit --wizard for bare scaffold"
        )


class WikiLayoutInitFailed(CollectionError):
    """WikiLayout.init() raised during auto-init."""

    def __init__(self, wiki_name: str, cause: BaseException) -> None:
        super().__init__(f"failed to auto-init wiki {wiki_name!r}: {cause}")
        self.wiki_name = wiki_name
        self.__cause__ = cause
