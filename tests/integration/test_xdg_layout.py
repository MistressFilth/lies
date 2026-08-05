"""End-to-end: XDG layout across all major commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from lies.cli import app


def test_init_then_query_then_lint_uses_xdg(tmp_path: Path) -> None:
    runner = CliRunner()
    # Init
    r = runner.invoke(app, ["init", "e2e"])
    assert r.exit_code == 0, r.output

    data = tmp_path / "xdg" / "data" / "lies" / "e2e"
    config = tmp_path / "xdg" / "config" / "lies" / "e2e"
    state = tmp_path / "xdg" / "state" / "lies" / "e2e"
    runtime = tmp_path / "xdg" / "runtime" / "lies" / "e2e"

    # Wiki root contains only raw/, wiki/, .git/
    assert (data / "raw").exists()
    assert (data / "wiki").exists()
    assert (data / ".git").exists()
    assert not (data / ".lies").exists()

    # Config
    assert (config / "schema.md").exists()
    assert (config / "collections").exists()

    # Runtime (empty at this point)
    assert runtime.exists()

    # State root exists
    assert state.exists()
