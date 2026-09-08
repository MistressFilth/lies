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
- Post-commit qmd hook (Task 12): when ``commit(..., qmd_collection=...)``
  is set, registers the per-collection library subdir with qmd,
  refreshes the derived index, and embeds the collection vectors —
  mirroring ``etl/stages/write.py``. Failures are non-fatal (the
  library commit already landed).
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    # Type-checker-only imports for the lazy ``__getattr__`` re-export
    # below. The runtime imports live in ``__getattr__`` so that
    # ``import lies.cli`` does not pull pydantic_ai / fastmcp into
    # ``sys.modules`` (pinned by ``test_cli_lazy_imports``).
    from lies.qmd.cli import (  # noqa: TC004
        qmd_collection_add_or_update,
        qmd_embed,
        qmd_update,
    )


def __getattr__(name: str):
    """Lazy import of qmd CLI helpers — keeps ``import lies.cli`` cheap.

    Importing :mod:`lies.qmd.cli` runs :mod:`lies.qmd`'s ``__init__``,
    which pulls in :mod:`lies.qmd.capability` and (via that module)
    :mod:`pydantic_ai` + :mod:`fastmcp`. ``LibraryWriter`` is loaded at
    CLI startup (via ``lies.library.__init__`` from ``lies.cli``), so
    importing qmd here would defeat the
    ``tests/unit/cli/test_cli_lazy_imports.py`` no-pydantic_ai /
    no-fastmcp contract. The qmd symbols are only needed when
    ``commit(..., qmd_collection=...)`` actually fires the post-commit
    hook, which is the rare ingest path — defer the heavy load until
    then.

    Mirrors the PEP 562 pattern already used by
    :mod:`lies.cli.page` / :mod:`lies.cli.ingestion` /
    :mod:`lies.mcp`. Tests using ``mock.patch(
    "lies.library.writer.qmd_collection_add_or_update", ...)`` still
    work because :func:`mock.patch` triggers this ``__getattr__`` on
    its initial ``getattr`` (caching the real function in module
    globals), then ``setattr``s the patched version on top — subsequent
    ``commit()`` calls read the patched name from globals.
    """
    if name in {
        "qmd_collection_add_or_update",
        "qmd_update",
        "qmd_embed",
    }:
        from lies.qmd.cli import (
            qmd_collection_add_or_update as _qmd_add_or_update,
            qmd_embed as _qmd_embed,
            qmd_update as _qmd_update,
        )

        globals()["qmd_collection_add_or_update"] = _qmd_add_or_update
        globals()["qmd_update"] = _qmd_update
        globals()["qmd_embed"] = _qmd_embed
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class LibraryWriter:
    def __init__(self, library: Library) -> None:
        self._library = library

    def commit(
        self,
        paths: Iterable[Path],
        *,
        message: str,
        catalog_updates: Iterable[LibraryCatalogPage] = (),
        qmd_collection: str | None = None,
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

        # Post-commit qmd hook: mirrors the wiki-side write.py contract.
        # Failures are non-fatal — the library commit already landed and
        # is authoritative. Skip on a no-op commit (sha is None) since
        # there is nothing new to register.
        if sha is not None and qmd_collection is not None:
            coll_dir = self._library.collections_root / qmd_collection
            # Positional ``path`` is the library ``collections_root``
            # (NOT ``coll_dir``) — the qmd hook resolves the registered
            # path via ``library_target`` (the collection dir); ``path``
            # is just a fallback used when ``library_target`` is not
            # set. Passing a deliberately distinct value here lets the
            # wire test distinguish the positional from the kwarg and
            # catch a regression that silently drops ``library_target``.
            try:
                qmd_collection_add_or_update(
                    self._library.git_root,
                    self._library.collections_root,
                    qmd_collection,
                    library_target=coll_dir,
                )
            except Exception as exc:  # noqa: BLE001 - qmd is derived; failures must not roll back the commit
                print(
                    f"warning: qmd collection registration failed for {qmd_collection!r}: {exc}; "
                    f"continuing (library commit stands). Run `lies status` for state.",
                    file=sys.stderr,
                )
            try:
                qmd_update(self._library.git_root)
            except Exception as exc:  # noqa: BLE001 - qmd is derived; failures must not roll back the commit
                print(
                    f"warning: qmd index update failed: {exc}; "
                    f"continuing (library commit stands). Run `lies status` for state.",
                    file=sys.stderr,
                )
            try:
                qmd_embed(self._library.git_root, qmd_collection)
            except Exception as exc:  # noqa: BLE001 - qmd is derived; failures must not roll back the commit
                print(
                    f"warning: qmd embed failed for {qmd_collection!r}: {exc}; "
                    f"continuing (library commit stands). Run `lies status` (or `qmd status` directly if status also fails) for state.",
                    file=sys.stderr,
                )

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
