"""Constrained Python tools for qmd and git operations."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def shell(*_args: object, **_kwargs: object) -> Any:
    """Reject the legacy arbitrary-shell capability.

    LIES exposes explicit Python wrappers instead of giving the model a shell
    allowlist. This compatibility shim makes accidental use fail closed.
    """
    raise RuntimeError("arbitrary shell access is disabled; use constrained tools")


def constrained_tools(repo: Path) -> list[Any]:
    """Return the explicit qmd/git tools bound to ``repo``."""
    from lies.qmd.cli import qmd_status, qmd_update
    from lies.wiki.git import git_log, git_status

    return [
        lambda: qmd_update(repo),
        lambda: qmd_status(repo),
        lambda: git_status(repo),
        lambda: git_log(repo),
    ]

