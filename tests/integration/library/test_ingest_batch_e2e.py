"""Integration test for ``lies ingest --batch <dir> --slug-prefix X``.

Pins the spec §"Atomic commit envelope": walks a directory, writes one
mirror per file, atomic-commit envelope returns a single commit.

Spec: ~/code/project-notes/lies/superpowers/specs/2026-09-08-deterministic-ingest-library-design.md
       L372 / L400 / "Integration suites".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.library.catalog import list_pages, open_catalog
from lies.library.paths import Library

pytestmark = pytest.mark.integration

runner = CliRunner()


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "t"],
        check=True,
    )


def _seed_inputs(src_dir: Path) -> None:
    """Three valid .md files + one LICENSE (filtered by stem gate)."""
    (src_dir / "overview.md").write_text("overview body\n" * 12)
    (src_dir / "deep-dive.md").write_text("deep dive body\n" * 12)
    (src_dir / "summary.md").write_text("summary body\n" * 12)
    (src_dir / "LICENSE").write_text("License\n" * 12)


def test_batch_e2e_writes_mirrors_and_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    _git_init(lib.git_root)

    src_dir = tmp_path / "in"
    src_dir.mkdir()
    _seed_inputs(src_dir)

    result = runner.invoke(
        app,
        [
            "ingest",
            "--batch",
            str(src_dir),
            "--slug-prefix",
            "claude",
        ],
    )

    assert result.exit_code == 0, f"CLI exit {result.exit_code}: {result.output}"
    coll_dir = lib.collections_root / "claude"
    written = sorted(p.name for p in coll_dir.iterdir() if p.is_file())
    assert written == [
        "deep-dive.md",
        "overview.md",
        "summary.md",
    ], f"mirror files mismatch: {written}"

    # Atomic commit envelope: single commit on the library repo with all
    # three mirrors tracked.
    git_log = subprocess.run(
        ["git", "-C", str(lib.git_root), "log", "--oneline"],
        check=True,
        capture_output=True,
        text=True,
    )
    commit_lines = [ln for ln in git_log.stdout.splitlines() if ln and not ln.startswith("init")]
    assert len(commit_lines) == 1, f"expected exactly 1 ingest commit, got {commit_lines}"
    assert "ingest:" in commit_lines[0], commit_lines[0]
    assert "claude" in commit_lines[0], commit_lines[0]

    # Catalog db holds one row per mirror under section=library.
    conn = open_catalog(lib)
    try:
        rows = list(list_pages(conn, section="library"))
    finally:
        conn.close()
    slugs = sorted(p.slug for p in rows)
    assert slugs == [
        "claude/deep-dive",
        "claude/overview",
        "claude/summary",
    ], slugs
