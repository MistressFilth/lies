from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
