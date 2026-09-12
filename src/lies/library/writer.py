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
import subprocess
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
        # Idempotent git repo bootstrap. A fresh library directory
        # (no ``.git``) gets a bootstrapped repo on first writer
        # creation so the atomic-commit envelope can land. Mirrors the
        # wiki-side ``git_init_initial`` precedent at
        # ``src/lies/wiki/layout.py:56``. Repos that are already
        # initialised are left alone — the ``.git`` subdir is the
        # short-circuit.
        #
        # Without this, the first ``lies ingest`` against a fresh
        # library wrote mirror files on disk but raised
        # ``LibraryAtomicCommitFailed`` at the ``git add`` step, and
        # the CLI exited 0 with the failure swallowed.
        if not (library.git_root / ".git").exists():
            self._bootstrap_git_repo()

    def _bootstrap_git_repo(self) -> None:
        """Initialise a git repo at ``library.git_root`` with a baseline commit.

        The library already has ``.lies/catalog.db`` from
        ``Library.open()``; that, plus the ``poison_root`` and
        ``collections_root`` directories, is what gets staged.
        """
        root = self._library.git_root
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(root)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "lies@localhost"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "lies"],
            check=True,
            capture_output=True,
            text=True,
        )
        # ``.lies/catalog.db`` may not exist yet on a brand-new library,
        # but the seed commit must contain something for ``git add .``
        # to be a non-empty stage. ``.gitkeep`` markers under the
        # poison_root / collections_root subdirs are the cheapest
        # non-functional content; remove them after the init commit
        # so the canonical layout stays clean.
        self._library.poison_root.mkdir(parents=True, exist_ok=True)
        self._library.collections_root.mkdir(parents=True, exist_ok=True)
        poison_keep = self._library.poison_root / ".gitkeep"
        collections_keep = self._library.collections_root / ".gitkeep"
        poison_keep.write_text("")
        collections_keep.write_text("")
        try:
            subprocess.run(
                ["git", "-C", str(root), "add", "."],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "commit", "-m", "init"],
                check=True,
                capture_output=True,
                text=True,
            )
        finally:
            # Tidy: never leave .gitkeep placeholders in poison / collections.
            if poison_keep.exists():
                poison_keep.unlink()
            if collections_keep.exists():
                collections_keep.unlink()

    def commit(
        self,
        paths: Iterable[Path],
        *,
        message: str,
        catalog_updates: Iterable[LibraryCatalogPage] = (),
        qmd_collection: str | None = None,
    ) -> str | None:
        # I12: accept both absolute and relative paths. Absolute paths
        # are coerced via ``relative_to(git_root)``; callers that
        # already pass repo-relative strings (or paths) round-trip
        # unchanged. A path that escapes ``git_root`` raises a typed
        # ``LibraryAtomicCommitFailed`` (we deliberately do not
        # silently stringify — the previous behaviour misclassified
        # caller mistakes as commit failures with no diagnostic).
        rel_paths: list[str] = []
        for p in paths:
            if isinstance(p, str):
                rel_paths.append(p)
                continue
            try:
                rel = p.relative_to(self._library.git_root)
            except ValueError:
                if p.is_absolute():
                    raise LibraryAtomicCommitFailed(
                        f"path {p} is outside git_root {self._library.git_root}; "
                        f"expected a repo-relative path or a path under git_root"
                    ) from None
                # Repo-relative already (relative_to errored because
                # of mismatched root); pass through as a string.
                rel_paths.append(str(p))
                continue
            rel_paths.append(str(rel))
        updates = list(catalog_updates)

        # Minor 27: symmetric short-circuit. ``atomic_commit`` rejects
        # ``files=[]`` as a programming error, so we cannot fall through
        # when one side of the envelope is empty. Two cases:
        #
        #   rel_paths=[] and updates=[] → true no-op (return None).
        #   rel_paths=[] and updates!=[] → catalog-only write. The
        #     catalog.db is committed via ``upsert_pages`` (see Minor 26);
        #     no git commit is needed. Still apply the catalog upsert so
        #     the catalog-side state stays consistent, then return None.
        #
        # This mirrors the wiki-side ``WikiMemoryService.apply_plan`` envelope
        # which short-circuits a no-op commit but still applies the
        # catalog-side state.
        if not rel_paths and not updates:
            return None
        if not rel_paths:
            self._upsert_catalog(updates)
            return None

        try:
            sha = atomic_commit(
                self._library.git_root,
                message,
                files=rel_paths,
            )
        except Exception as exc:
            # I13: include the recovery command in the error so the
            # operator can inspect the staged state without reading
            # source.
            raise LibraryAtomicCommitFailed(
                f"atomic_commit failed for {self._library.git_root}: {exc}. "
                f"Run `git -C {self._library.git_root} status` to inspect staged state."
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
            # Minor 26: ``upsert_pages`` commits internally (matches the
            # wiki-side ``lies.memory.catalog.upsert_pages`` contract). No
            # second commit needed — one fsync per call, regardless of how
            # many pages land.
            upsert_pages(conn, updates)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc):
                raise LibraryCatalogLocked(
                    f"library catalog busy_timeout exceeded at {self._library.catalog_path} during write"
                ) from exc
            raise
        finally:
            conn.close()


__all__ = ("LibraryWriter",)
