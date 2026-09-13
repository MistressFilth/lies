"""Wiki directory primitives (slim — XDG paths live on ``Wiki``)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lies.qmd.cli import qmd_collection_add_or_update


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

    def __init__(self, root: Path, name: str = "default") -> None:
        self.root = root
        self.name = name

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
        try:
            qmd_collection_add_or_update(
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
