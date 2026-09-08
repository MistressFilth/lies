"""Unit tests for ``lies status`` library-state surfacing (Task 15).

``lies status`` gains a ``library:`` line that reports the catalog
row count by section (``library`` + ``library-migrated``). The line
must surface even when no wiki is registered yet (the library is
independent of any specific wiki).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()


def test_status_reports_library_catalog_count(tmp_path: Path, monkeypatch) -> None:
    """``lies status`` surfaces a ``library:`` line with catalog counts.

    Sets up a real library directory (singleton ``Library`` rooted at
    ``xdg.data_home()/lies/library/``) with an empty catalog, plus a
    stubbed ``resolve_wiki`` so the wiki section doesn't crash on a
    fresh wiki. Asserts the rendered output contains the library line.
    """
    from lies import xdg
    from lies.library.paths import Library
    from lies.wiki.wiki import Wiki

    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    (lib_root / ".lies").mkdir()
    (lib_root / "collections").mkdir()
    monkeypatch.setattr(xdg, "data_home", lambda: lib_root)

    Library.open.cache_clear()
    lib = Library.open()
    lib.git_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(lib.git_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.email", "t@t"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.name", "t"],
        check=True,
        capture_output=True,
    )
    # Make an initial commit so the git repo is non-empty; an empty
    # initial tree plus the catalog DB created below would otherwise
    # leave git with nothing to commit (empty dirs aren't tracked).
    (lib.git_root / ".gitkeep").write_text("placeholder\n")
    subprocess.run(
        ["git", "-C", str(lib.git_root), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )

    # Stub ``resolve_wiki`` so the wiki section doesn't fail with
    # WikiNotRegistered; the library section must run regardless.
    wiki = Wiki(
        name="default",
        data_root=tmp_path / "wiki",
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )
    (tmp_path / "wiki" / "wiki").mkdir(parents=True)
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda _name=None: wiki)

    result = runner.invoke(app, ["status"], env={"LIES_WIKI_NAME": "default"})
    # ``section=library`` is the library-line substring; the wiki-side
    # ``catalog:`` line only says ``catalog:`` (no ``section=``), so this
    # assertion pins the library line specifically (not the wiki catalog
    # block that pre-existed).
    out = result.stdout.lower()
    assert "section=library" in out, f"library line missing from status output: {result.stdout!r}"
    # The library line is also exposed when the wiki section is reachable,
    # which it is here (resolve_wiki is stubbed). Both substrings present.
    assert "library" in out
    assert "catalog" in out
