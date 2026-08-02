"""Typed exceptions for the collections package."""

from __future__ import annotations


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
