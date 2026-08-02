"""Unit tests for scripts/worktree_lint.py.

Tests run against synthesized `git worktree list --porcelain` output
captured as a string, so they don't need a real git repo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure scripts/ is importable when running pytest from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from worktree_lint import lint


def _wrap(entries: list[str]) -> str:
    """Format porcelain entries as git would emit them.

    Each entry is a list of `key value` lines. Entries are separated
    by blank lines; the output ends with a trailing newline.
    """
    return "\n".join(entries) + "\n"


def test_conformant_single_worktree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A single sibling worktree with matching dir + branch + upstream passes."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "main"
    output = _wrap(
        [
            f"worktree {worktree_dir}",
            "HEAD abc123",
            "branch refs/heads/main",
        ]
    )
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: output.encode())
    violations = lint(bare)
    assert violations == []


def test_dir_branch_mismatch_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A worktree whose directory name does not match its branch fails."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "feat-something"
    output = _wrap(
        [
            f"worktree {worktree_dir}",
            "HEAD abc123",
            "branch refs/heads/different-branch",
        ]
    )
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: output.encode())
    violations = lint(bare)
    assert len(violations) >= 1
    assert any("feat-something" in v and "different-branch" in v for v in violations)


def test_nested_worktree_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A worktree under another worktree's .claude/worktrees/ fails."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "main" / ".claude" / "worktrees" / "feature-x"
    output = _wrap(
        [
            f"worktree {worktree_dir}",
            "HEAD abc123",
            "branch refs/heads/feature-x",
        ]
    )
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: output.encode())
    violations = lint(bare)
    assert any(".claude/worktrees" in v for v in violations)


def test_detached_head_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A worktree with a detached HEAD fails."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "orphan"
    output = _wrap(
        [
            f"worktree {worktree_dir}",
            "HEAD abc123",
            "detached",
        ]
    )
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: output.encode())
    violations = lint(bare)
    assert any("detached" in v.lower() for v in violations)


def test_origin_remote_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bare dir without origin remote fails the origin-url invariant."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "main"
    porcelain = _wrap(
        [
            f"worktree {worktree_dir}",
            "HEAD abc123",
            "branch refs/heads/main",
        ]
    )

    def fake_check_output(args: list[str], **kwargs: object) -> bytes:
        if any("remote" in a for a in args):
            return b""
        return porcelain.encode()

    monkeypatch.setattr("subprocess.check_output", fake_check_output)
    violations = lint(bare)
    assert any("origin" in v.lower() or "remote" in v.lower() for v in violations)


def test_fetch_refspec_must_be_direct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fetch refspec must be direct, not refs/remotes/origin/*."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "main"
    porcelain = _wrap(
        [
            f"worktree {worktree_dir}",
            "HEAD abc123",
            "branch refs/heads/main",
        ]
    )

    def fake_check_output(args: list[str], **kwargs: object) -> bytes:
        if "config" in args and "--get" in args:
            return b"+refs/heads/*:refs/remotes/origin/*\n"
        return porcelain.encode()

    monkeypatch.setattr("subprocess.check_output", fake_check_output)
    violations = lint(bare)
    assert any("refspec" in v.lower() for v in violations)


def test_upstream_mismatch_violation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A branch whose upstream tracks the wrong branch fails."""
    bare = tmp_path / "lies.git"
    worktree_dir = tmp_path / "feature-x"
    porcelain = _wrap(
        [
            f"worktree {worktree_dir}",
            "HEAD abc123",
            "branch refs/heads/feature-x",
        ]
    )

    config_output = (
        b'[branch "feature-x"]\n\tremote = origin\n\tmerge = refs/heads/wrong-upstream\n'
    )

    def fake_check_output(args: list[str], **kwargs: object) -> bytes:
        if "config" in args and "--get-regexp" in args:
            return config_output
        if "config" in args and "--get" in args:
            return b"+refs/heads/*:refs/heads/*\n"
        return porcelain.encode()

    monkeypatch.setattr("subprocess.check_output", fake_check_output)
    violations = lint(bare)
    assert any("upstream" in v.lower() or "wrong-upstream" in v for v in violations)
