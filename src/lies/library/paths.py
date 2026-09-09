"""Library paths: singleton rooted at xdg.data_home()/LIES_DATA_SUBDIR/library/."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from lies import xdg
from lies.constants import LIES_DATA_SUBDIR

_COLLECTION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class CollectionNameError(ValueError):
    """Raised when a ``LibraryCollection.name`` is invalid.

    Subclasses ``ValueError`` so callers that catch ``ValueError``
    (e.g. ``ingest._process_item``'s ``except ValueError: quarantine``)
    still see the failure — but with a more specific type for
    diagnostics. See Minor 40.
    """


def _validate_collection_name(name: str) -> str:
    if not _COLLECTION_NAME_RE.fullmatch(name):
        raise CollectionNameError(f"invalid collection name: {name!r}")
    return name


@dataclass(frozen=True)
class Library:
    """Singleton ingest destination. Sibling of any wiki's data root."""

    collections_root: Path
    catalog_path: Path
    log_path: Path
    poison_root: Path
    git_root: Path

    @classmethod
    @lru_cache(maxsize=1)
    def open(cls) -> Library:
        root = xdg.data_home() / LIES_DATA_SUBDIR / "library"
        return cls(
            collections_root=root / "collections",
            catalog_path=root / ".lies" / "catalog.db",
            log_path=root / "log.md",
            poison_root=root / "poison",
            git_root=root,
        )

    def collection(self, name: str) -> LibraryCollection:
        return LibraryCollection(library=self, name=name)


@dataclass(frozen=True)
class LibraryCollection:
    """A named collection directory under the library library.

    Minor 40: ``name`` is validated at construction time (via
    ``__post_init__``) so direct instantiation ``LibraryCollection(name=...)``
    cannot bypass the regex the way ``Library.collection(name)``-then-bypass
    could. ``__post_init__`` runs after the auto-generated ``__init__``;
    the dataclass is still ``frozen=True`` so the validation fires once
    and the name is immutable afterwards.
    """

    library: Library
    name: str

    def __post_init__(self) -> None:
        _validate_collection_name(self.name)

    @property
    def dir(self) -> Path:
        return self.library.collections_root / self.name

    @property
    def raw_dir(self) -> Path:
        return self.dir / "raw"

    @property
    def manifest_path(self) -> Path:
        return self.dir / ".lies" / "manifest.json"


__all__ = ("CollectionNameError", "Library", "LibraryCollection")
