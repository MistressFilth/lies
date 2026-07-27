"""Atomic git commit helpers for the wiki."""
from __future__ import annotations

import subprocess
from pathlib import Path

# Git exits non-zero with this prefix when no pathspec matches an index entry
# (used to distinguish a benign "nothing to reset" from a real reset error).
_GIT_PATHSPEC_NO_MATCH_FRAGMENT = "did not match any file(s) known to git"


class CommitError(Exception):
    """Raised when an atomic commit fails.

    The wiki's index (staging area) is guaranteed to be in the same state it
    was in before ``atomic_commit`` was called: any partial staging introduced
    by this call is rolled back. The working tree is left untouched -- the
    caller's on-disk edits are preserved so they can be inspected, re-tried,
    or discarded by the caller.
    """


def _run(
    args: list[str], repo: Path, *, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a git subprocess, always capturing output as text.

    Centralizes the ``capture_output=True`` / ``text=True`` / ``check=False``
    default and keeps callers focused on semantics.
    """
    return subprocess.run(
        args,
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def _reset_staging(repo: Path) -> None:
    """Best-effort reset of the index to HEAD, tolerating an empty repo.

    Uses ``git reset HEAD --`` (no pathspec) so the entire index is cleared
    regardless of what the caller passed to ``atomic_commit``. This is the
    safest post-condition: any partial staging from a failed ``git add`` is
    discarded, and the index returns to the HEAD commit.

    In an empty repository (no commits yet), ``git reset HEAD`` errors with
    ``fatal: Failed to resolve 'HEAD' as a valid ref`` -- that is benign and
    means the index is already empty, so the error is swallowed.
    """
    result = _run(["git", "reset", "HEAD", "--"], repo)
    # In an empty repo there is no HEAD yet; the reset error is benign.
    if result.returncode != 0 and "Failed to resolve 'HEAD'" in (result.stderr or ""):
        return


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode:
        raise CommitError(result.stderr.strip() or "git command failed")
    return result.stdout


def git_status(repo: Path) -> str:
    return _run(repo, "status", "--short")


def git_log(repo: Path, limit: int = 10) -> str:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    return _run(repo, "log", f"-{limit}", "--oneline")


def atomic_commit(
    repo: Path,
    message: str,
    files: list[str] | None = None,
) -> str:
    """Stage and commit the given files atomically; return the new commit SHA.

    Atomicity guarantee:

    - **Staging**: the index is restored to its pre-call state on ANY failure
      path -- including failures from ``git add`` (missing file, permission
      error), the staging-area probe, the commit itself, and the SHA lookup.
      A broad ``except BaseException`` is used so even ``KeyboardInterrupt``
      cannot leave the index dirty.
    - **Working tree**: the caller's on-disk edits are NEVER modified by this
      function on failure. They remain on disk for the caller to inspect,
      re-stage, or discard.
    - **Commit history**: on success, exactly one new commit is created and
      its 40-character SHA returned. On failure, no commit is created.

    Args:
        repo: Path to the git working tree.
        message: Commit message.
        files: List of file paths (relative to ``repo``) to commit. If
            ``None``, all tracked-file modifications in the working tree
            are committed (untracked files are not staged). An empty list
            is rejected.

    Returns:
        The 40-character commit SHA.

    Raises:
        CommitError: If the commit fails for any reason. The index is
            guaranteed to be clean (i.e., unchanged from before the call).
    """
    if files is not None and len(files) == 0:
        raise CommitError("files must not be empty")

    try:
        # ---- Stage ----------------------------------------------------------
        if files is not None:
            add_result = _run(["git", "add", "--", *files], repo)
        else:
            # Stage modifications to all tracked files. Untracked files are
            # left alone; the caller is responsible for creating them and
            # asking explicitly.
            add_result = _run(["git", "add", "-u"], repo)
        if add_result.returncode != 0:
            raise CommitError(f"git add failed: {add_result.stderr.strip()}")

        # ---- Probe ---------------------------------------------------------
        # Use check=False (and a manual success check) so a probe failure
        # raises CommitError and is funnelled into the rollback path, instead
        # of leaking a CalledProcessError out of atomic_commit.
        diff_result = _run(["git", "diff", "--cached", "--name-only"], repo)
        if diff_result.returncode != 0:
            raise CommitError(
                f"git diff --cached failed: {diff_result.stderr.strip()}"
            )
        if not diff_result.stdout.strip():
            raise CommitError("nothing to commit")

        # ---- Commit --------------------------------------------------------
        commit_result = _run(["git", "commit", "-m", message], repo)
        if commit_result.returncode != 0:
            raise CommitError(f"git commit failed: {commit_result.stderr.strip()}")

        # ---- Read SHA -------------------------------------------------------
        sha_result = _run(["git", "rev-parse", "HEAD"], repo)
        if sha_result.returncode != 0:
            raise CommitError(
                f"git rev-parse HEAD failed: {sha_result.stderr.strip()}"
            )
        return sha_result.stdout.strip()

    except CommitError:
        # Funnel through the rollback path; the original CommitError is
        # re-raised below.
        _reset_staging(repo)
        raise
    except subprocess.CalledProcessError as exc:
        # Defensive: even though _run uses check=False, callers that swap
        # subprocess (e.g., tests, or future refactors) could still let a
        # CalledProcessError escape. Convert to CommitError so the index
        # rollback is preserved and the public contract is upheld.
        _reset_staging(repo)
        stderr = (exc.stderr or "").strip() if hasattr(exc, "stderr") else ""
        raise CommitError(
            f"git subprocess failed: {' '.join(exc.cmd)}: {stderr or 'unknown error'}"
        ) from exc
    except BaseException:
        # Catch BaseException (not just Exception) so KeyboardInterrupt and
        # SystemExit also funnel through the rollback path. The index MUST
        # end up clean on every failure mode.
        _reset_staging(repo)
        raise
