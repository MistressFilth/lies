"""Atomic git commit helpers for the wiki."""
from __future__ import annotations

import subprocess
from pathlib import Path


class CommitError(Exception):
    """Raised when an atomic commit fails."""


def atomic_commit(
    repo: Path,
    message: str,
    files: list[str] | None = None,
) -> str:
    """Stage and commit the given files atomically; return the new commit SHA.

    If the commit fails, the staging area is reset and `CommitError` is raised.
    On success, the commit is created and its SHA returned.

    Args:
        repo: Path to the git working tree.
        message: Commit message.
        files: List of file paths (relative to `repo`) to commit. If None,
            all tracked-file modifications in the working tree are committed.
            An empty list is rejected.

    Returns:
        The 40-character commit SHA.

    Raises:
        CommitError: If the commit fails for any reason.
    """
    if files is not None and len(files) == 0:
        raise CommitError("files must not be empty")

    try:
        if files is not None:
            add_result = subprocess.run(
                ["git", "add", "--", *files],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            if add_result.returncode != 0:
                raise CommitError(f"git add failed: {add_result.stderr.strip()}")
        else:
            # Stage modifications to all tracked files. Untracked files are
            # left alone; the caller is responsible for creating them and
            # asking explicitly.
            add_result = subprocess.run(
                ["git", "add", "-u"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            if add_result.returncode != 0:
                raise CommitError(f"git add failed: {add_result.stderr.strip()}")

        # Check that staging has at least one entry
        diff_result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        if not diff_result.stdout.strip():
            raise CommitError("nothing to commit")

        commit_result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_result.returncode != 0:
            raise CommitError(f"git commit failed: {commit_result.stderr.strip()}")

        sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return sha_result.stdout.strip()

    except CommitError:
        # Roll back any staging introduced by this call.
        reset_args: list[str] = ["git", "reset", "HEAD", "--"]
        reset_args.extend(files or [])
        subprocess.run(
            reset_args,
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        raise
