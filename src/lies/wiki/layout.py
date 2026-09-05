"""Wiki directory primitives (slim — XDG paths live on ``Wiki``)."""

from __future__ import annotations

import shutil
import subprocess
from importlib import resources
from pathlib import Path


class WikiLayout:
    """Thin wrapper around the wiki's content directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def wiki_dir(self) -> Path:
        return self.root / "wiki"

    def init(self) -> None:
        """Create ``raw/`` and ``wiki/`` under ``root``."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)


def copy_default_schema(target: Path) -> None:
    """Copy the bundled default schema to ``target``."""
    src = resources.files("lies.schema").joinpath("default_schema.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(src), str(target))


def _gitignore_lines() -> tuple[str, ...]:
    """Lines seeded into a fresh wiki's ``.gitignore``.

    ``.lies/`` covers every runtime artifact under the sidecar
    directory. The explicit ``catalog.db*`` entries below are
    documentation of the sqlite catalog and its WAL siblings; the
    seeded ``.gitignore`` lives at the repo root, so the root-anchored
    patterns only ever apply if the operator widens or relocates the
    directory-wide rule (see P3 in the project TODO).
    """
    return (
        ".lies/",
        ".lies/memory_plans.jsonl",
        ".lies/catalog.db",
        ".lies/catalog.db-wal",
        ".lies/catalog.db-shm",
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
