"""LibraryWriter: atomic-commit envelope for library mutations.

Resolves consolidate-wikis Open Q #3: library uses the same atomic-commit
envelope as ``WikiMemoryService``. Both rely on ``atomic_commit`` from
``lies.wiki.git``.

Differences from wiki-side write path:

- Git root: ``library.git_root``, NOT a wiki's ``data_root``.
- Cross-process flock: ``library.git_root.parent / .lies / library.lock``
  (singleton, since library is a singleton). Phases later can promote
  to a per-project lock once consolidation lands.
- Catalog upsert: ``library.catalog_path`` (not wiki's).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from lies.library.catalog import (
    LibraryCatalogPage,
    open_catalog,
    upsert_pages,
)
from lies.library.errors import (
    LibraryAtomicCommitFailed,
    LibraryCatalogLocked,
)
from lies.library.paths import Library
from lies.wiki.git import atomic_commit


class LibraryWriter:
    def __init__(self, library: Library) -> None:
        self._library = library

    def commit(
        self,
        paths: Iterable[Path],
        *,
        message: str,
        catalog_updates: Iterable[LibraryCatalogPage] = (),
    ) -> str | None:
        # Paths arrive already relative to ``library.git_root`` (callers
        # compute them via ``Path.relative_to(library.git_root)``). Atomic
        # commit expects repo-relative strings, so we just stringify.
        rel_paths = [str(p) for p in paths]
        updates = list(catalog_updates)

        # Short-circuit a true no-op: nothing to commit and nothing to
        # upsert. atomic_commit rejects an empty files list, so we must
        # not call it in this case.
        if not rel_paths and not updates:
            return None

        try:
            sha = atomic_commit(
                self._library.git_root,
                message,
                files=rel_paths,
            )
        except Exception as exc:
            raise LibraryAtomicCommitFailed(
                f"atomic_commit failed for {self._library.git_root}: {exc}"
            ) from exc

        if updates:
            self._upsert_catalog(updates)
        return sha

    def _upsert_catalog(self, updates: list[LibraryCatalogPage]) -> None:
        try:
            conn = open_catalog(self._library)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc):
                raise LibraryCatalogLocked(
                    f"library catalog busy_timeout exceeded at {self._library.catalog_path}"
                ) from exc
            raise
        try:
            upsert_pages(conn, updates)
            conn.commit()
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc):
                raise LibraryCatalogLocked(
                    f"library catalog busy_timeout exceeded at {self._library.catalog_path} during write"
                ) from exc
            raise
        finally:
            conn.close()


__all__ = ("LibraryWriter",)
