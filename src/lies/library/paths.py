"""Library paths: singleton rooted at xdg.data_home()/LIES_DATA_SUBDIR/library/."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from lies import xdg
from lies.constants import LIES_DATA_SUBDIR

_COLLECTION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _validate_collection_name(name: str) -> str:
    if not _COLLECTION_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid collection name: {name!r}")
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
        _validate_collection_name(name)
        return LibraryCollection(library=self, name=name)


@dataclass(frozen=True)
class LibraryCollection:
    library: Library
    name: str

    @property
    def dir(self) -> Path:
        return self.library.collections_root / self.name

    @property
    def raw_dir(self) -> Path:
        return self.dir / "raw"

    @property
    def manifest_path(self) -> Path:
        return self.dir / ".lies" / "manifest.json"


__all__ = ("Library", "LibraryCollection")
