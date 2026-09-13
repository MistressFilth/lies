"""Wiki directory primitives (slim — XDG paths live on ``Wiki``)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lies.wiki.wiki import Wiki

# Sentinel file name: marks ``wiki_<name>`` as already registered with
# qmd. Lives at ``<wiki.data_root>/.lies/<name>``, alongside the catalog
# DB and sidecar. The directory is gitignored (see ``_gitignore_lines``)
# so the sentinel never lands in a commit.
QMD_REGISTRATION_SENTINEL = "wiki_qmd_registered"


def __getattr__(name: str):
    """Lazy import of the qmd registration helper.

    :mod:`lies.qmd.cli` pulls in :mod:`lies.qmd`'s ``__init__``, which
    transitively imports :mod:`pydantic_ai` + :mod:`fastmcp`. Keeping
    the import lazy means ``WikiLayout`` stays import-cheap for code
    paths that never reach the registration block (the existing
    ``test_wiki_layout.py`` smoke tests, the CLI's pre-init layout
    inspection, etc.).

    Mirrors the PEP 562 pattern in :mod:`lies.library.writer` /
    :mod:`lies.cli.page` / :mod:`lies.cli.ingestion` /
    :mod:`lies.mcp`. Tests that ``mock.patch(
    "lies.wiki.layout.qmd_collection_add_or_update", ...)`` rely on
    this ``__getattr__`` to materialise the module attribute on first
    access; the patch then ``setattr``s on top of the cached binding,
    so subsequent ``init()`` calls observe the patched function.
    """
    if name == "qmd_collection_add_or_update":
        from lies.qmd.cli import qmd_collection_add_or_update as _qmd_add_or_update

        globals()["qmd_collection_add_or_update"] = _qmd_add_or_update
        return _qmd_add_or_update
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class WikiLayout:
    """Thin wrapper around the wiki's content directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        # The wiki name is the data root's basename by convention
        # (``Wiki.data_root_for(name) = .../<LIES_DATA_SUBDIR>/<name>``).
        # Deriving it here keeps callers in sync with the qmd collection
        # name (``wiki_<name>``) the dispatcher queries.
        self.name = root.name

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def wiki_dir(self) -> Path:
        return self.root / "wiki"

    def init(self) -> None:
        """Create ``raw/`` and ``wiki/`` under ``root`` and register
        ``wiki_<name>`` with qmd so retrieval can query the wiki-rooted
        collection alongside library collections."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        # Register the wiki-rooted qmd collection so retrieval can
        # query it. Mirrors ``LibraryWriter.qmd_post_commit`` pattern
        # (PR #72). The ``qmd_collection_add_or_update`` helper is
        # idempotent: re-running init on an already-registered wiki is
        # a no-op when the path matches. The existing post-commit hook
        # (``WikiMemoryService._refresh_qmd``) calls ``qmd update``
        # which re-indexes this collection alongside the library
        # collections — no new post-commit hook is needed for the wiki
        # side.
        #
        # On success, the sentinel file is written atomically so the
        # first-write / first-query self-heal hook
        # (:func:`ensure_wiki_qmd_registered`) skips the subprocess on
        # subsequent operations. The sentinel is gitignored (see
        # ``_gitignore_lines``) so it never lands in a commit.
        try:
            # Resolve the lazy ``__getattr__`` binding explicitly: PEP 562
            # module-level ``__getattr__`` does NOT fire for
            # ``LOAD_GLOBAL`` inside a function/method body. The bare
            # name below would otherwise raise ``NameError`` in a fresh
            # process and emit a spurious warning. See
            # :func:`ensure_wiki_qmd_registered` for the same pattern.
            _qaou = globals().get("qmd_collection_add_or_update") or __getattr__(
                "qmd_collection_add_or_update"
            )
            _qaou(
                self.root,
                self.wiki_dir,
                f"wiki_{self.name}",
            )
        except Exception as exc:  # noqa: BLE001 - qmd is derived; init must not fail closed
            print(
                f"warning: qmd wiki collection registration failed for "
                f"'wiki_{self.name}': {exc}; queries will fall back to library-only. "
                f"Run `lies status` for state.",
                file=sys.stderr,
            )
            return
        _write_sentinel(self.root)


def qmd_sentinel_path(data_root: Path) -> Path:
    """Path to the per-wiki qmd-registration sentinel file."""
    return data_root / ".lies" / QMD_REGISTRATION_SENTINEL


def _write_sentinel(data_root: Path) -> None:
    """Atomically write the qmd-registration sentinel.

    Writes to ``<data_root>/.lies/.wiki_qmd_registered.tmp`` first and
    then renames into place so concurrent writers (the write path and
    the query path can race on a cold start) cannot truncate each
    other's sentinel mid-write. The parent ``.lies/`` directory is
    created on demand so callers do not have to pre-create it.
    """
    sentinel = qmd_sentinel_path(data_root)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    # ``os.replace`` is atomic on POSIX when source and destination are
    # on the same filesystem; the sentinel and its tmp live under the
    # same ``.lies/`` directory so that holds. ``fd, path`` pair avoids
    # a TOCTOU where a concurrent writer could delete + replace between
    # ``open`` and ``replace``.
    tmp = sentinel.with_name(sentinel.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.close(fd)
        os.replace(tmp, sentinel)
    except BaseException:
        # Tidy the tmp if rename failed (the tmp file may have been
        # left behind by a crashed writer).
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def ensure_wiki_qmd_registered(wiki: Wiki) -> bool:
    """Self-heal: register ``wiki_<name>`` with qmd if not yet registered.

    Wikis created before 0.22.0 have no ``wiki_<name>`` qmd collection
    because the ``WikiAlreadyExists`` guard in the init path blocked
    re-registration. This function is the migration hook: it is called
    from every wiki write (``WikiMemoryService.apply_plan``) and every
    MCP query (``Orchestrator.run_query``). On a cold start the
    sentinel is absent; we register via
    :func:`lies.qmd.cli.qmd_collection_add_or_update` and write the
    sentinel atomically. On every subsequent call the sentinel short
    circuits the subprocess.

    Failures are non-fatal: a qmd outage must not roll back the wiki
    commit or fail the answer. The function prints a warning to
    stderr and returns False; the absence of a written sentinel means
    the next call retries automatically.

    Returns:
        True if the collection is registered (sentinel existed or the
        registration attempt succeeded). False if the registration
        failed and a retry will happen on the next call.
    """
    sentinel = qmd_sentinel_path(wiki.data_root)
    if sentinel.exists():
        return True

    # Resolve the lazy ``__getattr__`` binding explicitly. ``LOAD_GLOBAL``
    # inside a function does NOT fall through to the module's
    # ``__getattr__`` (PEP 562); it only fires for ``module.name``
    # attribute access. Without the explicit lookup here the call
    # raises ``NameError`` in a fresh process. Same pattern as
    # :mod:`lies.library.writer`.
    _qaou = globals().get("qmd_collection_add_or_update") or __getattr__(
        "qmd_collection_add_or_update"
    )
    try:
        _qaou(
            wiki.data_root,
            wiki.wiki_dir,
            f"wiki_{wiki.name}",
        )
    except Exception as exc:  # noqa: BLE001 - qmd is derived; self-heal must not roll back the caller's commit / answer
        print(
            f"warning: qmd wiki collection self-heal failed for "
            f"'wiki_{wiki.name}': {exc}; next write/query will retry. "
            f"Run `lies status` for state.",
            file=sys.stderr,
        )
        return False

    try:
        _write_sentinel(wiki.data_root)
    except OSError as exc:
        # Sentinel write failed but the registration may have
        # succeeded; on the next call ``qmd_collection_add_or_update``
        # is idempotent (no-op when path matches), so the cost of a
        # re-attempt is bounded. Surface the warning so the operator
        # knows the wiki will keep retrying the qmd subprocess on
        # every write/query.
        print(
            f"warning: could not write qmd registration sentinel at {sentinel}: {exc}; "
            f"next write/query will retry the qmd subprocess.",
            file=sys.stderr,
        )
        return False
    return True


def copy_default_schema(target: Path) -> None:
    """Copy the bundled default schema to ``target``."""
    src = resources.files("lies.schema").joinpath("default_schema.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(src), str(target))


def _gitignore_lines() -> tuple[str, ...]:
    """Lines seeded into a fresh wiki's ``.gitignore``.

    ``.lies/`` covers every runtime artifact under the sidecar
    directory. The catalog itself lives at
    ``<wiki>/wiki/.lies/catalog.db`` (not at the wiki root), so the
    ``wiki/`` prefix on the ``catalog.db*`` pattern below is required
    to match — root-anchored entries never match the real path. The
    glob subsumes the database, WAL, and SHM siblings.
    """
    return (
        ".lies/",
        ".lies/memory_plans.jsonl",
        "wiki/.lies/catalog.db*",
    )


def git_init_initial(path: Path) -> None:
    """git init --initial-branch=main, set user.email/name, add ., commit."""
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "lies@localhost"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "lies"],
        check=True,
        capture_output=True,
        text=True,
    )
    # Exclude runtime artifacts under ``<wiki>/.lies/`` (the sidecar at
    # ``.lies/memory_plans.jsonl`` lives there). ``WikiMemoryService.apply_plan``
    # snapshots the working tree via ``git stash push --include-untracked``;
    # without this ignore the untracked sidecar is stashed and dropped on
    # success, silently losing prior sidecar lines.
    #
    # Guard against clobbering: an operator who curated a custom
    # ``.gitignore`` before re-running ``lies init`` must not lose their
    # entries.
    gitignore_path = path / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(
            "".join(f"{line}\n" for line in _gitignore_lines()), encoding="utf-8"
        )
    subprocess.run(
        ["git", "-C", str(path), "add", "."],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "initial wiki"],
        check=True,
        capture_output=True,
        text=True,
    )
