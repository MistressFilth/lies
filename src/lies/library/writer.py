<<<<<<< HEAD
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
=======
"""LibraryWriter: atomic-commit envelope for library mutations."""
>>>>>>> 7f2597a (docs(src): bring every code docstring within the 12-line budget)

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
<<<<<<< HEAD
    """Lazy import of qmd CLI helpers — keeps ``import lies.cli`` cheap.

    Importing :mod:`lies.qmd.cli` pulls in :mod:`pydantic_ai` +
    :mod:`fastmcp` transitively. ``LibraryWriter`` is loaded at CLI
    startup, so importing qmd here would defeat the
    ``tests/unit/cli/test_cli_lazy_imports.py`` no-pydantic_ai /
    no-fastmcp contract. The qmd symbols are only needed when
    ``commit(..., qmd_collection=...)`` fires the post-commit hook, which
    is the rare ingest path — defer the heavy load until then.

    Mirrors the PEP 562 pattern used by :mod:`lies.cli.page`. Tests using
    ``mock.patch("lies.library.writer.qmd_collection_add_or_update", ...)``
    still work because :func:`mock.patch` triggers this ``__getattr__`` on
    its initial ``getattr``, then ``setattr``s the patched version.
    """
=======
    """Lazy import of qmd CLI helpers — keeps ``import lies.cli`` cheap."""
>>>>>>> 7f2597a (docs(src): bring every code docstring within the 12-line budget)
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
<<<<<<< HEAD
        # Idempotent git repo bootstrap. A fresh library directory
        # (no ``.git``) gets a bootstrapped repo on first writer
        # creation so the atomic-commit envelope can land; without it the
        # first ``lies ingest`` writes mirror files to disk and then
        # raises ``LibraryAtomicCommitFailed`` at the ``git add`` step.
=======
        # Idempotent git bootstrap: a fresh library (no ``.git``) gets a repo on first
        # writer creation so the atomic-commit envelope can land. An existing ``.git``
        # short-circuits. Without it the first ``lies ingest`` raised
        # ``LibraryAtomicCommitFailed`` at ``git add`` with the CLI still exiting 0.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
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
<<<<<<< HEAD
        # Accept both absolute and relative paths. Absolute paths
        # are coerced via ``relative_to(git_root)``; callers that
        # already pass repo-relative strings (or paths) round-trip
        # unchanged. A path that escapes ``git_root`` raises a typed
        # ``LibraryAtomicCommitFailed`` rather than being stringified,
        # which would misclassify a caller mistake as a commit failure.
=======
        # Accept absolute and relative paths: absolutes coerce via ``relative_to``.
        # A path escaping ``git_root`` raises a typed ``LibraryAtomicCommitFailed``
        # rather than being stringified — a caller mistake is not a commit failure.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
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

<<<<<<< HEAD
        # Symmetric short-circuit. ``atomic_commit`` rejects
        # ``files=[]`` as a programming error, so we cannot fall through
        # when one side of the envelope is empty. Two cases:
        #
        #   rel_paths=[] and updates=[] → true no-op (return None).
        #   rel_paths=[] and updates!=[] → catalog-only write, applied
        #     so the catalog-side state stays consistent, then None.
        #
        # This mirrors the wiki-side ``WikiMemoryService.apply_plan`` envelope.
=======
        # Symmetric short-circuit, because ``atomic_commit`` rejects ``files=[]``.
        # Both sides empty is a true no-op (return None); ``rel_paths=[]`` with
        # updates is a catalog-only write, so the upsert still runs and then None —
        # mirroring the wiki-side ``apply_plan`` envelope.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
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

<<<<<<< HEAD
        # Reconcile deletions: ``atomic_commit`` runs ``git add -- <files>``
        # for the explicit paths only, so a tracked file that was previously
        # committed and then deleted from disk is never staged, and every
        # re-ingest accumulates ghost entries in HEAD. ``git add -u`` (NOT
        # ``-A``) stages only modifications and deletions of already-tracked
        # files, leaving new untracked files alone — ``-A`` would also pick
        # up files the writer does not claim.
=======
        # Reconcile deletions: ``atomic_commit`` stages the explicit paths only, so a
        # tracked file deleted from disk never gets staged and every re-ingest
        # accumulates ghosts in HEAD. ``git add -u`` stages modifications and
        # deletions of tracked files, leaving untracked ones to the caller.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
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

<<<<<<< HEAD
        # Mark that a library has been written since the qmd daemon started.
        # Touched at the machine-global path so the daemon's staleness check
        # reflects the union of every library + wiki write envelope, not just
        # the current git_root. Best-effort and idempotent; the parent may not
        # exist on a fresh install, so mkdir -p it first. Goes BEFORE the qmd
        # post-commit hook so the daemon's mtime-vs-marker comparison sees
        # this commit's mtime, not the hook's.
=======
        # Touch the machine-global qmd last-write-marker so the daemon's staleness
        # check reflects every library + wiki write, not just this git_root.
        # Best-effort and idempotent; mkdir -p first. Goes BEFORE the qmd
        # post-commit hook so the mtime comparison sees this commit, not the hook's.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
        if sha is not None:
            try:
                _cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
                _marker = _cache / "qmd" / "last-write-marker"
                _marker.parent.mkdir(parents=True, exist_ok=True)
                _marker.touch(exist_ok=True)
            except OSError:
                pass  # F14 is best-effort; do not fail the commit

<<<<<<< HEAD
        # Post-commit qmd hook. Failures are non-fatal — the library
        # commit already landed and is authoritative. Skip on a no-op commit
        # (sha is None) since there is nothing new to register.
        #
        # Bare-name resolution matters here. The qmd helpers are exposed
        # via :PEP:`562` module-level ``__getattr__`` (so ``import lies``
        # does not pull pydantic_ai / fastmcp into ``sys.modules``).
        # Python ``LOAD_GLOBAL`` bytecode does NOT consult
        # ``__getattr__`` — bare-name lookup inside a function body
        # raises ``NameError`` against ``module.__dict__``. Mirror the
        # workaround :mod:`lies.cli.page` uses: explicit dict access with
        # manual ``__getattr__`` re-invocation on miss.
        if sha is not None and qmd_collection is not None:
            coll_dir = self._library.collections_root / qmd_collection
            # Positional ``path`` is the library ``collections_root``
            # (NOT ``coll_dir``) — the qmd hook resolves the registered
            # path via ``library_target`` (the collection dir); ``path``
            # is just a fallback used when ``library_target`` is not set.
            # Passing a deliberately distinct value lets the wire test
            # distinguish the positional from the kwarg.
=======
        # Post-commit qmd hook, mirroring the wiki-side write.py contract. Failures
        # are non-fatal: the commit already landed and is authoritative. Skipped on
        # a no-op commit (sha is None). Bare-name resolution matters — the helpers
        # are :PEP:`562` lazy, and ``LOAD_GLOBAL`` skips ``__getattr__``; see ``lies.cli.page``.
        if sha is not None and qmd_collection is not None:
            coll_dir = self._library.collections_root / qmd_collection
            # Positional ``path`` is the library ``collections_root``, not ``coll_dir``;
            # the hook resolves the registered path via ``library_target``. A deliberately
            # distinct value lets the wire test catch a dropped ``library_target``.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
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
<<<<<<< HEAD
                # Re-raise with the spec-mandated message that names the
                # holder PID and tells the operator to wait-and-retry, so the
                # agent sees an actionable message instead of
                # ``qmd embed failed; continuing (library commit stands)``.
                # The format is guarded against ``waited_s``/``max_s``/
                # ``holder_pid`` being ``None`` (the type signature accepts
                # None for translation paths like ``WikiFlockIndeterminate``).
=======
                # Re-raise with the spec-mandated message naming the holder PID and
                # wait-and-retry, so the agent sees an actionable error. The format is
                # guarded against ``None`` fields: today's ``_acquire_with_poll`` always
                # populates floats, but a non-timeout source could crash the re-raise.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
                waited_str = f"{exc.waited_s:.1f}" if exc.waited_s is not None else "unknown"
                max_str = f"{exc.max_s:.0f}" if exc.max_s is not None else "unknown"
                holder_str = str(exc.holder_pid) if exc.holder_pid is not None else "unknown"
                # ``lock_path`` is forwarded because the flock is
                # *resolvable*: naming the inode is the one operator step
                # that tells a contended host from a broken one, and
                # ``LIES_QMD_LOCK_PATH`` can override it, so the path in
                # the message is the only record of *which* inode was
                # contended.
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
