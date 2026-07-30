"""Wiki directory layout and path conventions.

A wiki root contains:
    raw/                # immutable sources
    wiki/               # LLM-owned markdown
        index.md
        log.md
        overview.md
        <page-type>/<name>.md
        ...
    .lies/
        schema.md       # per-wiki schema override (optional)
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WikiLayout:
    """Resolved paths for a LIES wiki rooted at `root`."""

    root: Path

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def wiki_dir(self) -> Path:
        return self.root / "wiki"

    @property
    def lies_dir(self) -> Path:
        return self.root / ".lies"

    @property
    def schema_path(self) -> Path:
        return self.lies_dir / "schema.md"

    @property
    def index_path(self) -> Path:
        return self.wiki_dir / "index.md"

    @property
    def log_path(self) -> Path:
        return self.wiki_dir / "log.md"

    @property
    def overview_path(self) -> Path:
        return self.wiki_dir / "overview.md"

    @property
    def lint_report_path(self) -> Path:
        return self.wiki_dir / "lint-report.md"

    def page_path(self, page_type: str, name: str) -> Path:
        """Return the path for a wiki page of the given type and name."""
        return self.wiki_dir / page_type / f"{name}.md"

    def is_git_repo(self) -> bool:
        """Return True iff the wiki root is a git working tree."""
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        toplevel = Path(result.stdout.strip())
        try:
            return toplevel.resolve() == self.root.resolve()
        except FileNotFoundError:
            return False

    def init(self) -> None:
        """Initialize the wiki directory structure (does NOT create a git repo).

        Caller is responsible for `git init` separately. Also ensures
        ``<root>/.gitignore`` contains ``.lies/memory.lock`` so
        ``git stash push --include-untracked`` never unlinks the
        inode behind a held cross-process flock.
        """
        for directory in (self.raw_dir, self.wiki_dir, self.lies_dir):
            directory.mkdir(parents=True, exist_ok=True)
        # Lazy import to avoid a circular dependency: service.py imports
        # WikiLayout at module scope.
        from lies.memory.service import _ensure_lock_gitignored

        _ensure_lock_gitignored(self.root / ".lies" / "memory.lock")
