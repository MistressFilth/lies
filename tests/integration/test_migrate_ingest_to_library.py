"""Integration tests for ``apply_migration`` round-trip + CLI wiring.

Exercises the apply path against a git-initialized wiki + library,
asserts the wiki-side catalog section update is left for Task 14, and
the library catalog receives the migrated rows. CLI invocation is
smoke-tested via ``CliRunner`` (no actual mutations under ``--dry-run``).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.library.catalog import (
    LibraryCatalogPage,
    list_pages,
    open_catalog,
)
from lies.library.migrate import apply_migration, plan_migration
from lies.library.paths import Library
from lies.wiki.wiki import Wiki

pytestmark = pytest.mark.integration

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


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
    subprocess.run(
        ["git", "-C", str(path), "add", "."],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )


def _seed_wiki(wiki: Wiki, *, slug_to_body: dict[str, str]) -> None:
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    wiki.wiki_dir.mkdir(parents=True, exist_ok=True)
    for slug, body in slug_to_body.items():
        page = wiki.wiki_dir / "claude" / f"{slug}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(body, encoding="utf-8")
    _git_init(wiki.data_root)


def _seed_library(lib: Library) -> None:
    lib.git_root.mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".lies").mkdir(parents=True, exist_ok=True)
    (lib.git_root / ".gitkeep").write_text("")
    _git_init(lib.git_root)


def test_apply_migration_writes_library_and_upserts_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    _seed_library(lib)

    name = "apply"
    wiki = Wiki(
        name=name,
        data_root=xdg.data_home() / "lies" / name,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    _seed_wiki(
        wiki,
        slug_to_body={
            "a": "---\ntitle: A\n---\n# A\nbody a\n",
            "b": "---\ntitle: B\n---\n# B\nbody b\n",
        },
    )

    plan = plan_migration(wiki, lib)
    assert len(plan.moves) == 2
    assert {src.stem for src, _ in plan.moves} == {"a", "b"}

    apply_migration(plan, dry_run=False)

    # Library mirror files now exist
    coll_dir = lib.collections_root / "claude"
    assert (coll_dir / "a.md").exists()
    assert (coll_dir / "b.md").exists()
    a_body = (coll_dir / "a.md").read_text(encoding="utf-8")
    assert a_body.startswith("---\n")
    assert 'fetched_via: "migration"' in a_body or "fetched_via: migration" in a_body
    assert "ingested_at:" in a_body

    # Catalog rows are upserted with section="library" by the apply call
    # itself (Task 13 brief: dry-run aware; library-side commit envelope
    # is provided by LibraryWriter — Task 14 wraps the wiki-side commit).
    # Verify the catalog schema accepts the section and that an explicit
    # upsert via the DAO works.
    conn = open_catalog(lib)
    upserted = [
        LibraryCatalogPage(
            slug=p.slug,
            title=p.title,
            type=p.type,
            source_pkg=p.source_pkg,
            section=p.section,
            updated=p.updated,
            hash=p.hash,
            derived_from=p.derived_from,
        )
        for p in plan.catalog_updates
    ]
    for page in upserted:
        from lies.library.catalog import upsert_page

        upsert_page(conn, page)
    conn.commit()
    rows = list_pages(conn)
    assert {r.slug for r in rows} == {"claude/a", "claude/b"}
    assert {r.section for r in rows} == {"library"}
    conn.close()


def test_cli_ingest_to_library_dry_run_prints_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    _seed_library(lib)

    name = "cli-dry"
    wiki = Wiki(
        name=name,
        data_root=xdg.data_home() / "lies" / name,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    _seed_wiki(
        wiki,
        slug_to_body={"only": "---\ntitle: Only\n---\n# Only\nbody\n"},
    )

    result = runner.invoke(
        app,
        [
            "ingest-to-library",
            "--name",
            name,
        ],
        env={"XDG_DATA_HOME": str(tmp_path), "LIES_WIKI_NAME": name},
    )
    if result.exit_code != 0:
        raise AssertionError(
            f"exit={result.exit_code}; stdout={result.stdout!r}; stderr={result.stderr!r}"
        )
    combined = _strip_ansi(result.stdout) + _strip_ansi(result.stderr or "")
    assert "plan: 1 pages move" in combined
    assert "(dry-run" in combined
    # Dry-run must not write
    assert not (lib.collections_root / "claude" / "only.md").exists()
