"""Shared pytest fixtures."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from lies.wiki.layout import WikiLayout

FIXTURE_WIKI = Path(__file__).parent / "fixtures" / "sample-wiki"


@pytest.fixture(autouse=True)
def _isolated_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reset LIES / XDG env and redirect all XDG roots to tmp_path.

    Each test starts with a clean slate: every ``LIES_*``, ``LIES_XDG_*``,
    and ``XDG_*`` variable is removed, and the five XDG base directories
    are redirected to ``tmp_path/xdg/<role>/``. The wiki and pipeline
    code resolves its storage through these roots, so per-test isolation
    prevents tests from leaking state into the developer's real
    ``~/.local/share/lies`` etc.

    Also strips ``GIT_DIR`` from the environment. Pre-commit (and the
    bare-repo-with-worktrees layout) sets ``GIT_DIR`` to the parent
    worktree's gitdir when invoking ``git commit``; ``subprocess.run`` in
    tests inherits that, so ``git rev-parse --show-toplevel`` resolves
    to the worktree instead of the fresh tmp dir and breaks tests that
    assert the tmp dir is *not* a git repo. Clearing it here means the
    test subprocess sees the tmp dir's git state directly.
    """
    for key in [
        "LIES_MODEL",
        "LIES_WIKI_NAME",
        "LIES_WIKI_ROOT",
        "LIES_QMD_TRANSPORT",
        "LIES_QMD_URL",
        "LIES_LOG_LEVEL",
        "LIES_XDG_DATA_HOME",
        "LIES_XDG_CONFIG_HOME",
        "LIES_XDG_CACHE_HOME",
        "LIES_XDG_STATE_HOME",
        "LIES_XDG_RUNTIME_DIR",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "GIT_DIR",
    ]:
        monkeypatch.delenv(key, raising=False)
    xdg_root = tmp_path / "xdg"
    for sub in ("data", "config", "cache", "state", "runtime"):
        (xdg_root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_root / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_root / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_root / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg_root / "runtime"))


@pytest.fixture
def sample_wiki(tmp_path: Path) -> WikiLayout:
    """A copy of the sample fixture wiki initialised as a git working tree.

    Returns the :class:`WikiLayout` rooted at the tmp copy so tests can
    call ``synthesize_answer`` (and other wiki-scoped APIs) against a
    real on-disk corpus.
    """
    target = tmp_path / "wiki"
    shutil.copytree(FIXTURE_WIKI, target)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return WikiLayout(target)


@pytest.fixture
def empty_wiki(tmp_path: Path) -> WikiLayout:
    """An empty wiki (no ``wiki/index.md``) so fallback has nothing to read."""
    target = tmp_path / "wiki"
    target.mkdir()
    (target / "raw").mkdir()
    (target / "wiki").mkdir()
    (target / ".lies").mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return WikiLayout(target)


@pytest.fixture
def wiki_with_missing_pages(tmp_path: Path) -> WikiLayout:
    """A wiki whose ``wiki/index.md`` references pages that don't exist on disk.

    Three references: two ghosts (``ghost-1.md``, ``ghost-2.md``) and one
    real page (``entities/real.md``). Only the real page should appear in
    the synthesizer's citations; the ghosts are silently skipped.
    """
    target = tmp_path / "wiki"
    shutil.copytree(FIXTURE_WIKI, target)
    (target / "wiki" / "entities").mkdir(parents=True, exist_ok=True)
    (target / "wiki" / "entities" / "real.md").write_text(
        "# Real\n\nA real page that exists on disk.\n",
        encoding="utf-8",
    )
    (target / "wiki" / "index.md").write_text(
        "# Index\n\n"
        "- [Ghost1](entities/ghost-1.md) — missing\n"
        "- [Real](entities/real.md) — exists\n"
        "- [Ghost2](concepts/ghost-2.md) — missing\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return WikiLayout(target)
