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
        """Create raw/, wiki/; ensure data_root exists."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)


def copy_default_schema(target: Path) -> None:
    """Copy the bundled default schema to ``target``."""
    src = resources.files("lies.schema").joinpath("default_schema.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(src), str(target))


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
