"""LibraryWriter: atomic-commit envelope for library mutations.

Differences from the wiki-side write path:

- Git root: ``library.git_root``, NOT a wiki's ``data_root``.
- Cross-process flock: ``library.git_root.parent / .lies / library.lock``
  (singleton, since library is a singleton).
- Catalog upsert: ``library.catalog_path`` (not wiki's).
- Post-commit qmd hook: when ``commit(..., qmd_collection=...)`` is set,
  registers the per-collection library subdir with qmd, refreshes the
  derived index, and embeds the collection vectors. Failures are
  non-fatal (the library commit already landed).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import warnings
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
from lies.lock_errors import QmdLockBusy
from lies.wiki.git import atomic_commit

if TYPE_CHECKING:
    # Type-checker-only imports for the lazy ``__getattr__`` re-export
    # below. The runtime imports live in ``__getattr__`` so that
    # ``import lies.cli`` does not pull pydantic_ai / fastmcp into
    # ``sys.modules`` (pinned by ``test_cli_lazy_imports``).
    pass


def __getattr__(name: str):
    """Lazy import of qmd CLI helpers — keeps ``import lies.cli`` cheap.

    :mod:`lies.qmd.cli` pulls in :mod:`pydantic_ai` + :mod:`fastmcp`, and
    ``LibraryWriter`` loads at CLI startup, so importing it here would defeat
    the no-``pydantic_ai``/no-``fastmcp`` contract in
    ``test_cli_lazy_imports.py``. The symbols are needed only when
    ``commit(..., qmd_collection=...)`` fires the post-commit hook — the rare
    ingest path.

    Mirrors the :pep:`562` pattern in :mod:`lies.cli.page`. ``mock.patch``
    still works because it triggers this ``__getattr__`` on its initial
    ``getattr``, then ``setattr``s the patched value.
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
        # Idempotent git repo bootstrap: without it the first ``lies
        # ingest`` writes mirror files and then fails at ``git add``.
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
        # ``.lies/catalog.db`` may not exist on a brand-new library, but the seed
        # commit needs a non-empty stage. ``.gitkeep`` markers under the poison and
        # collections roots are the cheapest content; removed after the init commit.
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
        # Absolute paths are coerced via ``relative_to(git_root)``;
        # repo-relative ones round-trip. An escape raises a typed
        # ``LibraryAtomicCommitFailed`` rather than a stringified path.
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

        # Mirrors the wiki-side ``WikiMemoryService.apply_plan``. Both
        # sides empty is a true no-op; an empty ``rel_paths`` with
        # updates is a catalog-only write, applied so the catalog
        # stays consistent. ``atomic_commit`` rejects ``files=[]``.
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

        # Reconcile deletions: ``atomic_commit`` stages the explicit paths
        # only, so a deleted tracked file is never staged and every
        # re-ingest accumulates ghosts in HEAD. ``-u`` not ``-A``:
        # ``-A`` would stage files the writer does not claim.
        if sha is not None:
            try:
                subprocess.run(
                    ["git", "-C", str(self._library.git_root), "add", "-u"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except subprocess.CalledProcessError as exc:
                warnings.warn(
                    f"library: deletion reconcile `git add -u` failed: "
                    f"{(exc.stderr or '').strip() or exc}; "
                    f"tracked-file deletions may accumulate in HEAD until a "
                    f"manual `git -C {self._library.git_root} add -u && git "
                    f"commit` runs.",
                    stacklevel=2,
                )
            else:
                # If ``git add -u`` introduced further staged changes the caller did not
                # name, fold them into a follow-up commit so the deletions land atomically
                # with the original write.
                diff = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self._library.git_root),
                        "diff",
                        "--cached",
                        "--name-only",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if diff.stdout.strip():
                    try:
                        subprocess.run(
                            [
                                "git",
                                "-C",
                                str(self._library.git_root),
                                "commit",
                                "-m",
                                f"{message} (reconcile deletions)",
                            ],
                            check=True,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                    except subprocess.CalledProcessError as exc:
                        warnings.warn(
                            f"library: deletion reconcile commit failed: "
                            f"{(exc.stderr or '').strip() or exc}; "
                            f"tracked-file deletions may accumulate in HEAD until a "
                            f"manual `git -C {self._library.git_root} commit` runs.",
                            stacklevel=2,
                        )

        # Machine-global path, so staleness sees every library + wiki write;
        # BEFORE the post-commit hook so the mtime compared is this
        # commit's, not the hook's.
        if sha is not None:
            try:
                _cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
                _marker = _cache / "qmd" / "last-write-marker"
                _marker.parent.mkdir(parents=True, exist_ok=True)
                _marker.touch(exist_ok=True)
            except OSError:
                pass  # F14 is best-effort; do not fail the commit

        # Post-commit qmd hook; non-fatal, skipped on a no-op commit. Bare
        # names cannot resolve: the helpers live behind a :PEP:`562`
        # module ``__getattr__`` (keeping ``import lies`` lazy) which
        # ``LOAD_GLOBAL`` does not consult — see ``lies.cli.page``.
        if sha is not None and qmd_collection is not None:
            coll_dir = self._library.collections_root / qmd_collection
            # Positional ``path`` is ``collections_root``, not
            # ``coll_dir``: the hook resolves the real target via
            # ``library_target``. Distinct values let the wire test
            # tell the positional from the kwarg.
            _qaou = globals().get("qmd_collection_add_or_update") or __getattr__(
                "qmd_collection_add_or_update"
            )
            _qupd = globals().get("qmd_update") or __getattr__("qmd_update")
            _qemb = globals().get("qmd_embed") or __getattr__("qmd_embed")
            try:
                _qaou(
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
                _qupd(self._library.git_root)
            except Exception as exc:  # noqa: BLE001 - qmd is derived; failures must not roll back the commit
                print(
                    f"warning: qmd index update failed: {exc}; "
                    f"continuing (library commit stands). Run `lies status` for state.",
                    file=sys.stderr,
                )
            try:
                _qemb(self._library.git_root, qmd_collection)
            except QmdLockBusy as exc:
                # Names the holder PID and tells the operator to wait-and-retry,
                # so the agent sees an actionable message instead of
                # "embed failed; continuing". Guarded against ``None``,
                # which the signature accepts on translation paths.
                waited_str = f"{exc.waited_s:.1f}" if exc.waited_s is not None else "unknown"
                max_str = f"{exc.max_s:.0f}" if exc.max_s is not None else "unknown"
                holder_str = str(exc.holder_pid) if exc.holder_pid is not None else "unknown"
                # The flock is resolvable, so name the inode: it is the one
                # operator step that tells a contended host from a
                # broken one, and ``LIES_QMD_LOCK_PATH`` can override it.
                where = f" at {exc.lock_path}" if exc.lock_path else ""
                message = (
                    f"qmd embed lock contention: another process holds the qmd flock{where} "
                    f"(holder PID {holder_str}, waited {waited_str}s, max {max_str}s). "
                    f"Run `lies flock qmd status` to inspect the live holder, "
                    f"or wait and retry the operation."
                )
                raise QmdLockBusy(
                    holder_pid=exc.holder_pid,
                    waited_s=exc.waited_s,
                    max_s=exc.max_s,
                    lock_path=exc.lock_path,
                    message=message,
                ) from exc
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
            # ``upsert_pages`` commits internally (matching the wiki-side
            # ``lies.memory.catalog.upsert_pages`` contract), so no second
            # commit is needed — one fsync per call, regardless of how many
            # pages land.
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
