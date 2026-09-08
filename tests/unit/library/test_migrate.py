"""Tests for the one-shot library migration (Task 13).

Covers ``plan_migration`` + ``apply_migration`` dry-run semantics. The
apply round-trip is covered by ``tests/integration/test_migrate_ingest_to_library.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lies.library.migrate import plan_migration, apply_migration


@pytest.fixture
def wiki_with_pages(tmp_path: Path) -> Path:
    wiki_root = tmp_path / "wiki"
    wiki_dir = wiki_root / "wiki"
    wiki_dir.mkdir(parents=True)
    coll = wiki_dir / "claude"
    coll.mkdir()
    (coll / "a.md").write_text("---\ntitle: A\ntype: entity\n---\n# A\nbody\n")
    (coll / "b.md").write_text("---\ntitle: B\n---\n# B\nbody\n")
    subprocess.run(["git", "init", "-b", "main", str(wiki_root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wiki_root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wiki_root), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(wiki_root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(wiki_root), "commit", "-m", "init"], check=True, capture_output=True
    )
    return wiki_root


def test_plan_migration_lists_wiki_to_library(
    wiki_with_pages: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    monkeypatch.setattr("lies.wiki.wiki.xdg.data_home", lambda: tmp_path / "data")

    from lies.library.paths import Library

    Library.open.cache_clear()

    # Need Wiki dataclass for plan_migration(wiki, ...)
    from lies.wiki.wiki import Wiki

    wiki = Wiki(
        name="test",
        data_root=wiki_with_pages,
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )
    lib = Library.open()
    plan = plan_migration(wiki, lib, date_str="2026-09-08")
    assert len(plan.moves) == 2
    for src, dst in plan.moves:
        assert src.stem in {"a", "b"}
        assert dst.parent == lib.collections_root / "claude"


def test_apply_migration_dry_run_writes_nothing(
    wiki_with_pages: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lies import xdg

    lib_data = tmp_path / "lib"
    lib_data.mkdir()
    monkeypatch.setattr(xdg, "data_home", lambda: lib_data)
    from lies.library.paths import Library

    Library.open.cache_clear()
    from lies.wiki.wiki import Wiki

    wiki = Wiki(
        name="t",
        data_root=wiki_with_pages,
        config_root=tmp_path / "c",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "s",
        runtime_root=tmp_path / "r",
    )
    lib = Library.open()
    plan = plan_migration(wiki, lib)
    apply_migration(plan, lib, dry_run=True)
    coll_dir = lib.collections_root / "claude"
    assert not coll_dir.exists() or not any(coll_dir.iterdir())
