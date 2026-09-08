"""Wire test: LibraryWriter.commit calls qmd hooks against the library path.

The writer's post-commit hook mirrors the wiki-side hook
(``etl/stages/write.py``), but with the path rooted at
``library.collections_root / <collection>`` rather than
``wiki.wiki_dir / <collection>``. Real ``qmd`` shell-outs are slow +
flaky in CI, so the test monkey-patches ``qmd_collection_add_or_update``
(plus ``qmd_update`` and ``qmd_embed``) and asserts the writer routes
through the new ``library_target=`` kwarg.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from lies.library.catalog import LibraryCatalogPage
from lies.library.writer import LibraryWriter


def test_writer_calls_qmd_hook_against_library_path(tmp_path: Path) -> None:
    lib_root = tmp_path / "library"
    lib_root.mkdir()
    (lib_root / ".lies").mkdir()
    (lib_root / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-b", "main", str(lib_root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(lib_root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(lib_root), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(lib_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(lib_root), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )

    coll_dir = lib_root / "collections" / "claude"
    coll_dir.mkdir(parents=True)
    mirror = coll_dir / "x.md"
    mirror.write_text("body\n")

    called: dict[str, object] = {"args": None}

    def fake_qmd(*args, **kwargs):
        called["args"] = (args, kwargs)

    class _StubLib:
        git_root = lib_root
        collections_root = lib_root / "collections"
        catalog_path = lib_root / ".lies" / "catalog.db"

    with patch("lies.library.writer.atomic_commit", return_value="deadbeef" * 5):
        with patch("lies.library.writer.qmd_collection_add_or_update", side_effect=fake_qmd):
            with patch("lies.library.writer.qmd_update"):
                with patch("lies.library.writer.qmd_embed"):
                    writer = LibraryWriter(_StubLib())  # type: ignore[arg-type]
                    writer.commit(
                        [mirror.relative_to(lib_root)],
                        message="x",
                        catalog_updates=[
                            LibraryCatalogPage(
                                slug="claude/x",
                                title="X",
                                type="",
                                source_pkg="claude",
                                section="library",
                                updated="",
                                hash="",
                                derived_from="",
                            ),
                        ],
                        qmd_collection="claude",
                    )

    assert called["args"] is not None
    args, kwargs = called["args"]
    # qmd_collection_add_or_update(library_root, target_path, qmd_name,
    #                             library_target=...)
    assert kwargs.get("library_target") == coll_dir
    assert args[0] == lib_root
    assert args[1] == coll_dir
    assert args[2] == "claude"
