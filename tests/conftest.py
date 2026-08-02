"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lies.wiki.layout import WikiLayout

FIXTURE_WIKI = Path(__file__).parent / "fixtures" / "sample-wiki"


@pytest.fixture(autouse=True)
def reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each test starts with a clean LIES_* env."""
    for key in list(os.environ):
        if key.startswith("LIES_"):
            monkeypatch.delenv(key, raising=False)


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
