from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from lies.wiki import git as wiki_git
from lies.wiki.git import CommitError, atomic_commit


@pytest.fixture
def git_wiki(tmp_path: Path) -> Path:
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "initial.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


def test_atomic_commit_succeeds(git_wiki: Path) -> None:
    (git_wiki / "new.txt").write_text("hello")
    sha = atomic_commit(git_wiki, "add new file", files=["new.txt"])
    assert len(sha) == 40
    # Verify the commit exists
    result = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=git_wiki, capture_output=True, text=True, check=True
    )
    assert "add new file" in result.stdout


def test_atomic_commit_rolls_back_on_failure(git_wiki: Path) -> None:
    # Create a file that won't be in the commit list; commit should not touch it
    (git_wiki / "untouched.txt").write_text("untouched")
    (git_wiki / "new.txt").write_text("hello")
    # Calling with a non-existent file in the list should raise
    with pytest.raises(CommitError):
        atomic_commit(git_wiki, "bad", files=["nonexistent.txt"])
    # Working tree should be restored: untouched.txt still present, new.txt may or may not be
    assert (git_wiki / "untouched.txt").exists()


def test_atomic_commit_empty_tree(git_wiki: Path) -> None:
    # No changes; should produce a clean "no-op" or raise a specific error
    with pytest.raises(CommitError, match="nothing to commit"):
        atomic_commit(git_wiki, "no-op")


def test_atomic_commit_stages_tracked_modifications(git_wiki: Path) -> None:
    # When files=None, the contract is to commit all working-tree changes.
    (git_wiki / "initial.txt").write_text("updated content")
    sha = atomic_commit(git_wiki, "update tracked file")
    assert len(sha) == 40
    result = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=git_wiki, capture_output=True, text=True, check=True
    )
    assert "update tracked file" in result.stdout


def test_atomic_commit_empty_files_rejected(git_wiki: Path) -> None:
    (git_wiki / "new.txt").write_text("hello")
    with pytest.raises(CommitError, match="files must not be empty"):
        atomic_commit(git_wiki, "bad", files=[])
    # Staging area should remain clean
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=git_wiki,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_atomic_commit_rolls_back_staging_on_failure(git_wiki: Path) -> None:
    (git_wiki / "untouched.txt").write_text("untouched")
    (git_wiki / "new.txt").write_text("hello")
    with pytest.raises(CommitError):
        atomic_commit(git_wiki, "bad", files=["nonexistent.txt"])
    # Staging area must be clean
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=git_wiki,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""
    # Untouched file must still be present
    assert (git_wiki / "untouched.txt").exists()


# ---------------------------------------------------------------------------
# Safe rollback tests (Finding 9)
#
# `atomic_commit` must leave the index (staging area) in the same state it
# was in before the call, on EVERY failure path -- not just the ones that
# raise CommitError explicitly. The original implementation leaked
# CalledProcessError from `git diff --cached --name-only` and
# `git rev-parse HEAD` (both run with check=True), leaving the index dirty
# on those paths. These tests pin the corrected behaviour.
# ---------------------------------------------------------------------------


def _staged_files(repo: Path) -> list[str]:
    """Return the list of files currently in the index, sorted."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _completed_proc(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> mock.Mock:
    """Build a fake ``subprocess.CompletedProcess`` for monkey-patching."""
    proc = mock.Mock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_atomic_commit_rolls_back_when_probe_raises_calledprocesserror(
    git_wiki: Path,
) -> None:
    """A failure in the staging probe must NOT leak CalledProcessError and
    must leave the index clean.

    The probe (``git diff --cached --name-only``) used to be called with
    ``check=True``, so a git error there would escape as
    ``subprocess.CalledProcessError`` and skip the rollback. With the
    corrected implementation, the probe failure is caught, a
    ``CommitError`` is raised, and the index is reset.
    """
    (git_wiki / "new.txt").write_text("hello")

    real_run = subprocess.run

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        # Trip the probe before any commit can happen.
        if args[:3] == ["git", "diff", "--cached"]:
            raise subprocess.CalledProcessError(
                returncode=128,
                cmd=args,
                output="fatal: simulated probe failure",
                stderr="fatal: simulated probe failure",
            )
        return real_run(args, **kwargs)

    with (
        mock.patch.object(wiki_git.subprocess, "run", side_effect=fake_run),
        pytest.raises(CommitError, match="git diff --cached"),
    ):
        atomic_commit(git_wiki, "bad", files=["new.txt"])

    # Staging area must be clean (the partial add of new.txt was rolled back).
    assert _staged_files(git_wiki) == []


def test_atomic_commit_rolls_back_when_sha_lookup_fails(git_wiki: Path) -> None:
    """A failure looking up the new HEAD SHA must roll back staging.

    The ``git rev-parse HEAD`` call after a successful commit used to be
    ``check=True``, so a failure there would escape as
    ``CalledProcessError`` and leave any in-flight staging dirty. The
    contract is: a single commit happens, OR the index is clean.
    """
    (git_wiki / "new.txt").write_text("hello")

    real_run = subprocess.run

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            raise subprocess.CalledProcessError(
                returncode=128,
                cmd=args,
                output="fatal: simulated rev-parse failure",
                stderr="fatal: simulated rev-parse failure",
            )
        return real_run(args, **kwargs)

    with (
        mock.patch.object(wiki_git.subprocess, "run", side_effect=fake_run),
        pytest.raises(CommitError, match="git rev-parse HEAD"),
    ):
        atomic_commit(git_wiki, "bad", files=["new.txt"])

    assert _staged_files(git_wiki) == []


def test_atomic_commit_rolls_back_on_keyboard_interrupt(git_wiki: Path) -> None:
    """A ``KeyboardInterrupt`` during the commit path must also roll back.

    The rollback runs in an ``except BaseException`` so even an interrupt
    cannot leave the index dirty. This pins that the function is safe to
    use from a REPL where Ctrl-C is a realistic event.
    """
    (git_wiki / "new.txt").write_text("hello")

    real_run = subprocess.run

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[:2] == ["git", "commit"]:
            raise KeyboardInterrupt()
        return real_run(args, **kwargs)

    with (
        mock.patch.object(wiki_git.subprocess, "run", side_effect=fake_run),
        pytest.raises(KeyboardInterrupt),
    ):
        atomic_commit(git_wiki, "ctrl-c", files=["new.txt"])

    assert _staged_files(git_wiki) == []


def test_atomic_commit_rolls_back_partial_staging_from_failing_add(
    git_wiki: Path,
) -> None:
    """If ``git add`` partially stages and then fails, the partial staging
    is rolled back so the index is clean.

    With the corrected reset (``git reset HEAD --`` with no pathspec), any
    partial staging is cleared regardless of which file was the offender.
    """
    (git_wiki / "real.txt").write_text("present")

    real_run = subprocess.run

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        if args[:2] == ["git", "add"] and "real.txt" in args:
            # Simulate: the first file was staged, the second one (a
            # non-existent file) caused git add to fail.
            subprocess_run = real_run(
                ["git", "add", "--", "real.txt"],
                cwd=kwargs.get("cwd", git_wiki),
                capture_output=True,
                text=True,
            )
            if subprocess_run.returncode != 0:
                return subprocess_run
            return _completed_proc(
                returncode=128,
                stderr="fatal: pathspec 'ghost.txt' did not match any files",
            )
        return real_run(args, **kwargs)

    with (
        mock.patch.object(wiki_git.subprocess, "run", side_effect=fake_run),
        pytest.raises(CommitError, match="git add failed"),
    ):
        atomic_commit(
            git_wiki, "bad", files=["real.txt", "ghost.txt"]
        )

    # Even though real.txt was partially staged before the failure, the
    # rollback clears it.
    assert _staged_files(git_wiki) == []
