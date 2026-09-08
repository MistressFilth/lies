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

import pytest

from lies.library.catalog import LibraryCatalogPage
from lies.library.paths import Library
from lies.library.writer import LibraryWriter


@pytest.fixture
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Library:
    """Redirect ``xdg.data_home`` so ``Library.open()`` returns a per-test Library."""
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    yield Library.open()


@pytest.fixture
def lib_with_git(lib: Library) -> Library:
    """Initialise a git repo at ``library.git_root`` with a baseline commit.

    Mirror of ``test_writer.lib_with_git``: ``.gitkeep`` placeholder
    inside the empty ``.lies/`` so ``git add .`` has something to stage.
    """
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".lies").mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("")
    subprocess.run(
        ["git", "init", "-b", "main", str(lib.git_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(lib.git_root), "config", "user.name", "t"],
        check=True,
    )
    subprocess.run(["git", "-C", str(lib.git_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(lib.git_root), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return lib


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
    # The writer passes ``collections_root`` (NOT ``coll_dir``) as the
    # positional ``path`` so the test can catch a regression that
    # silently drops ``library_target`` — without this asymmetry, both
    # positional and kwarg would equal ``coll_dir`` and the kwarg's
    # role in registration would be invisible to the test. ``path`` is
    # the qmd fallback used only when ``library_target`` is not set;
    # passing a deliberately distinct value keeps the wire honest.
    assert args[0] == lib_root
    assert args[1] == lib_root / "collections"
    assert args[2] == "claude"
    # Library_target must be present as a kwarg with the collection dir.
    # Direct subscript (not ``.get``) so a regression that drops the
    # kwarg KeyErrors instead of silently passing.
    assert "library_target" in kwargs
    assert kwargs["library_target"] == coll_dir


def test_writer_qmd_hook_failure_logs_stderr_and_returns_sha(
    lib_with_git: Library,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """qmd hook failures must surface as stderr warnings; commit still lands.

    Regression for the silent-failure fix in ``writer.py``: the three qmd
    hooks (register / update / embed) run inside independent ``try``
    blocks that emit ``warning: qmd ...`` lines to ``sys.stderr`` on
    failure. The library commit must still land (return a real sha) so
    the authoritative state survives a derived-index outage. Without the
    stderr assertions, a regression that reverts ``print(..., file=
    sys.stderr)`` back to ``pass``, swaps stderr to stdout, or rebundles
    the hooks under one outer ``try`` would never trip a test.
    """
    coll_dir = lib_with_git.collections_root / "claude"
    coll_dir.mkdir(parents=True, exist_ok=True)
    mirror = coll_dir / "page.md"
    mirror.write_text("body\n")

    def fake_qmd_raise(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("qmd daemon down")

    monkeypatch.setattr("lies.library.writer.qmd_collection_add_or_update", fake_qmd_raise)
    monkeypatch.setattr("lies.library.writer.qmd_update", fake_qmd_raise)
    monkeypatch.setattr("lies.library.writer.qmd_embed", fake_qmd_raise)

    writer = LibraryWriter(lib_with_git)
    sha = writer.commit(
        [mirror.relative_to(lib_with_git.git_root)],
        message="ingest: claude +1",
        qmd_collection="claude",
    )

    # Commit must still land (real sha from a real ``atomic_commit``).
    assert sha is not None
    assert len(sha) == 40

    # Stderr must surface the three failures. The exact warnings name
    # each failing step (register / index / embed), so the substring
    # match is intentionally loose: "qmd" + "warning" is enough to fail
    # a regression that swaps the destination to stdout, drops the
    # ``print`` entirely, or rebundles the hooks under one outer ``try``
    # (which would re-raise and surface as a traceback instead of the
    # ``warning: ...`` shape).
    captured = capsys.readouterr()
    assert "qmd" in captured.err.lower()
    assert "warning" in captured.err.lower()
    # Stdout must remain empty — the warnings go to stderr only.
    assert captured.out == ""
