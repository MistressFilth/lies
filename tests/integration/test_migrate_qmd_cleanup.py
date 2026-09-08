"""Pins that ``lies ingest-to-library --apply`` runs the qmd cleanup
hook (wiki collection remove + library collection add + update + embed)
on every migrated collection.

Subcommand path matches Task 13's cli_migrate.py registration at the
root app under name ``ingest-to-library`` (top-level command, not nested
under a migrate group; a migrate group is a follow-up once we have a
second migration script).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from lies.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def migrated_wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a one-page wiki + empty library under a shared xdg root.

    The library lives at ``<xdg>/lies/library/`` (per ``Library.open()``)
    and the wiki "t" lives at ``<xdg>/lies/t/wiki/`` (per
    ``Wiki.require("t")``). Sharing one ``xdg.data_home`` lets the
    ``migrated_wiki`` fixture route both to ``tmp_path/library_data``
    so a single ``monkeypatch.setattr`` resolves wiki + library.
    """
    lib_root = tmp_path / "library_data"
    wiki_data_root = lib_root / "lies" / "t"
    wiki_dir = wiki_data_root / "wiki"
    wiki_dir.mkdir(parents=True)
    (wiki_dir / "claude").mkdir()
    (wiki_dir / "claude" / "x.md").write_text(
        "---\ntitle: X\ntype: entity\n---\n# X\nbody\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(wiki_data_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki_data_root), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki_data_root), "config", "user.name", "t"],
        check=True,
    )
    subprocess.run(["git", "-C", str(wiki_data_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(wiki_data_root), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr("lies.xdg.data_home", lambda: lib_root)
    from lies.library.paths import Library

    Library.open.cache_clear()
    return wiki_data_root


def test_post_apply_runs_qmd_cleanup(migrated_wiki: Path) -> None:
    called: list[tuple[str, tuple, dict]] = []
    with patch("lies.library.cli_migrate.atomic_commit", return_value="abc"):
        with patch(
            "lies.qmd.cli.qmd_collection_remove",
            side_effect=lambda *a, **kw: called.append(("remove", a, kw)),
        ):
            with patch(
                "lies.qmd.cli.qmd_collection_add_or_update",
                side_effect=lambda *a, **kw: called.append(("add", a, kw)),
            ):
                with patch(
                    "lies.qmd.cli.qmd_update",
                    side_effect=lambda *a, **kw: called.append(("update", a, kw)),
                ):
                    with patch(
                        "lies.qmd.cli.qmd_embed",
                        side_effect=lambda *a, **kw: called.append(("embed", a, kw)),
                    ):
                        # Top-level subcommand path (no ``migrate`` group yet).
                        result = runner.invoke(
                            app,
                            [
                                "ingest-to-library",
                                "--apply",
                                "--name",
                                "t",
                                "--date",
                                "2026-09-08",
                            ],
                            env={"LIES_WIKI_NAME": "t"},
                        )
    if result.exit_code != 0:
        raise AssertionError(
            f"exit={result.exit_code}; stdout={result.stdout!r}; stderr={result.stderr!r}"
        )
    ops = [c[0] for c in called]
    assert "remove" in ops, f"qmd_collection_remove not called; got ops={ops}"
    assert "add" in ops, f"qmd_collection_add_or_update not called; got ops={ops}"
    assert "update" in ops, f"qmd_update not called; got ops={ops}"
    assert "embed" in ops, f"qmd_embed not called; got ops={ops}"

    # Wiki collection name uses the ``<wiki>_<collection>`` convention;
    # the qmd_collection_remove first positional is the wiki data_root.
    remove_calls = [c for c in called if c[0] == "remove"]
    assert any(len(c[1]) >= 2 and c[1][1] == "t_claude" for c in remove_calls), (
        f"expected wiki collection 't_claude' to be removed; got {remove_calls}"
    )
