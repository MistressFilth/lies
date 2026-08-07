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
