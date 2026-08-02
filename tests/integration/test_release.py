"""End-to-end test for scripts/release.py against a throwaway bare repo.

Sets up a fake `origin` (local bare repo), clones it into a worktree,
seeds conventional commits, and runs release.py with a `git` wrapper
in PATH that intercepts `git push` so the test never touches the real
remote.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    full_env.setdefault("GIT_AUTHOR_NAME", "Test")
    full_env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    full_env.setdefault("GIT_COMMITTER_NAME", "Test")
    full_env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    return subprocess.check_output(["git", *args], cwd=cwd, env=full_env).decode("utf-8")


def _real_git_path() -> str:
    """Return the absolute path to the real `git` binary.

    Bypasses any PATH override we install for the script under test.
    """
    return shutil.which("git") or "/usr/bin/git"


def _install_git_wrapper(fake_bin: Path, push_log: Path) -> None:
    """Drop a `git` shell wrapper in ``fake_bin`` that records push calls.

    All other git invocations pass through to the real git binary at
    the path returned by :func:`_real_git_path`.
    """
    real = _real_git_path()
    wrapper = fake_bin / "git"
    # Use a here-doc with the real path baked in.
    wrapper.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "push" ]; then\n'
        f'  echo "$@" >> "{push_log}"\n'
        f"  exit 0\n"
        f"fi\n"
        f'exec "{real}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


@pytest.fixture
def throwaway_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create (bare_remote, working_clone) under tmp_path."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(bare))
    _git(tmp_path, "clone", str(bare), str(work))
    # Configure fake user identity in the clone.
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    # Seed minimal project structure so the script can find pyproject.toml
    # and src/lies/__init__.py (the script hardcodes the lies package path).
    (work / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "0.0.1"\n',
        encoding="utf-8",
    )
    (work / "src" / "lies").mkdir(parents=True)
    (work / "src" / "lies" / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    (work / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- Initial\n",
        encoding="utf-8",
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "chore: initial")
    _git(work, "push", "-u", "origin", "main")
    return bare, work


def test_release_pipeline_bumps_to_0_1_0(throwaway_repo: tuple[Path, Path]) -> None:
    _, work = throwaway_repo
    # Seed a feat commit so bump detection picks "minor".
    (work / "newfile.txt").write_text("x", encoding="utf-8")
    _git(work, "add", "newfile.txt")
    _git(work, "commit", "-m", "feat: add new file")
    _git(work, "push", "origin", "main")

    # Install a git wrapper that records push calls instead of pushing.
    fake_bin = work.parent / "fake-bin"
    fake_bin.mkdir()
    push_log = work.parent / "push.log"
    _install_git_wrapper(fake_bin, push_log)

    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(SCRIPTS_DIR) + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "release.py")],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"release failed: stdout={result.stdout} stderr={result.stderr}"

    # Version was bumped to 0.1.0
    pyproject = (work / "pyproject.toml").read_text(encoding="utf-8")
    assert '"0.1.0"' in pyproject
    init = (work / "src" / "lies" / "__init__.py").read_text(encoding="utf-8")
    assert '"0.1.0"' in init

    # CHANGELOG split happened
    changelog = (work / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.1.0]" in changelog

    # Tag exists
    tags = _git(work, "tag", "--list").strip().splitlines()
    assert "v0.1.0" in tags

    # Push was called (intercepted by the wrapper)
    push_log_contents = push_log.read_text(encoding="utf-8") if push_log.exists() else ""
    assert any(
        "push" in line and "origin" in line and "v0.1.0" in line
        for line in push_log_contents.splitlines()
    )
