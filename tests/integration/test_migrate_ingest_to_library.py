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

    apply_migration(plan, lib, dry_run=False)

    # Library mirror files now exist
    coll_dir = lib.collections_root / "claude"
    assert (coll_dir / "a.md").exists()
    assert (coll_dir / "b.md").exists()
    a_body = (coll_dir / "a.md").read_text(encoding="utf-8")
    assert a_body.startswith("---\n")
    assert 'fetched_via: "migration"' in a_body or "fetched_via: migration" in a_body
    assert "ingested_at:" in a_body

    # Catalog rows are upserted by apply_migration itself with
    # section="library" and a deterministic `updated` derived from
    # the source_hash (per spec §Migration §catalog state).
    conn = open_catalog(lib)
    try:
        rows = list_pages(conn)
        assert {r.slug for r in rows} == {"claude/a", "claude/b"}
        assert {r.section for r in rows} == {"library"}
    finally:
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


def test_cli_ingest_to_library_apply_writes_files_and_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    _seed_library(lib)

    name = "cli-apply"
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

    result = runner.invoke(
        app,
        [
            "ingest-to-library",
            "--apply",
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

    # Mirror files land at <library>/collections/<collection>/<slug>.md
    coll_dir = lib.collections_root / "claude"
    assert (coll_dir / "a.md").exists()
    assert (coll_dir / "b.md").exists()

    # Catalog row exists in <library>/.lies/catalog.db with section="library"
    conn = open_catalog(lib)
    try:
        rows = list_pages(conn)
        assert {r.slug for r in rows} == {"claude/a", "claude/b"}
        assert {r.section for r in rows} == {"library"}
    finally:
        conn.close()

    # CLI ran to completion. Wiki-side commit SHA is printed when the
    # wiki has tracked changes (e.g. via migration-backup writes for
    # duplicates); Task 13 scope doesn't add wiki-side cleanup, so
    # absent wiki-side changes → no SHA. Library-side atomic-commit
    # envelope is owned by LibraryWriter (wired in Task 14).
    assert "done." in combined
    sha_match = re.search(r"wiki commit ([0-9a-f]+)", combined)
    if sha_match is not None:
        assert len(sha_match.group(1)) >= 7


def test_apply_migration_atomic_commits_library_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2 anti-tautology: ``apply_migration`` lands a NEW library git commit.

    Before the fix, mirror files were written and the catalog was
    SQLite-committed, but no library git commit fired — leaving
    ``library.git_root`` with uncommitted mirror content. The new
    contract routes through ``LibraryWriter.commit`` so the spec
    §Migration §Atomicity requirement ("Per-collection library write =
    one atomic git commit at the library") is satisfied.
    """
    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    _seed_library(lib)

    name = "atomic-commit"
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
        slug_to_body={"a": "---\ntitle: A\n---\n# A\nbody\n"},
    )

    # Capture the lib git log before applying.
    before = subprocess.run(
        ["git", "-C", str(lib.git_root), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    before_count = len([line for line in before.splitlines() if line])

    plan = plan_migration(wiki, lib)
    assert len(plan.moves) == 1
    lib_sha = apply_migration(plan, lib, dry_run=False)

    assert lib_sha is not None, (
        "apply_migration must return a library commit SHA; mirror files were "
        "written but the library side was not committed"
    )
    assert len(lib_sha) == 40

    # A new commit must have landed in the library.
    after = subprocess.run(
        ["git", "-C", str(lib.git_root), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    after_count = len([line for line in after.splitlines() if line])
    assert after_count == before_count + 1, (
        f"expected one new library commit; before={before_count}, after={after_count}\n"
        f"log:\n{after}"
    )
    # The new commit message matches the migration tag.
    assert "ingest-to-library" in after.splitlines()[0]


def test_apply_migration_removes_duplicate_from_wiki(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3 anti-tautology: byte-identical duplicates are REMOVED from wiki.

    Two byte-identical files in different collections: only the
    first-seen survives in the library; the second moves to
    ``<wiki>/.lies/migration-backup/<date>/<coll>/<slug>.md`` AND is
    unlinked from ``<wiki>/wiki/<coll>/<slug>.md`` so the wiki-side
    atomic-commit picks up the removal.
    """
    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    _seed_library(lib)

    name = "dup-remove"
    wiki = Wiki(
        name=name,
        data_root=xdg.data_home() / "lies" / name,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    # Two byte-identical files in different collections.
    body = "---\ntitle: Shared\n---\n# Shared\nbody\n"
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    wiki.wiki_dir.mkdir(parents=True, exist_ok=True)
    for coll, slug in [("claude", "x"), ("openai", "y")]:
        page = wiki.wiki_dir / coll / f"{slug}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(body, encoding="utf-8")
    _git_init(wiki.data_root)

    plan = plan_migration(wiki, lib, date_str="2026-09-08")
    assert len(plan.moves) == 1  # first-seen wins
    assert len(plan.duplicates_to_backup) == 1
    src, backup = plan.duplicates_to_backup[0]
    assert src.exists()

    apply_migration(plan, lib, dry_run=False)

    # First-seen winner still exists in library.
    assert (lib.collections_root / "claude" / "x.md").exists()
    # Duplicate was removed from wiki AND copied to the backup path.
    assert not src.exists(), f"duplicate wiki file {src} was not removed"
    assert backup.exists(), f"backup file {backup} was not created"


def test_migrated_mirror_source_path_is_host_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I9: migrated mirror ``source_path:`` is a stable placeholder, not a host path.

    Previously the field carried ``str(src.resolve())`` — the host-
    local wiki path. That made migrated mirror files host-dependent:
    moving the wiki directory changes the byte-identical determinism
    contract. New value: ``"migrated-from-wiki"`` (stable).
    """
    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    _seed_library(lib)

    name = "host-indep"
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
    plan = plan_migration(wiki, lib)
    apply_migration(plan, lib, dry_run=False)

    mirror_text = (lib.collections_root / "claude" / "only.md").read_text(encoding="utf-8")
    assert 'source_path: "migrated-from-wiki"' in mirror_text or (
        "source_path: migrated-from-wiki" in mirror_text
    ), f"migrated mirror must carry stable placeholder; got:\n{mirror_text[:400]}"
    # And the host path does NOT appear.
    assert str(wiki.data_root.resolve()) not in mirror_text


def test_cli_collection_filter_narrows_all_plan_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I10: ``--collection=claude`` filters moves, duplicates, AND catalog updates.

    The previous filter only narrowed ``plan.moves`` — duplicates
    and catalog updates for other collections still shipped through.
    New contract: every plan field narrows on the same predicate.
    """
    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    lib = Library.open()
    _seed_library(lib)

    name = "filter"
    wiki = Wiki(
        name=name,
        data_root=xdg.data_home() / "lies" / name,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    wiki.wiki_dir.mkdir(parents=True, exist_ok=True)
    # claude/x and openai/y are byte-identical — openai/y is the
    # duplicate-backup target. Filter on ``claude`` and only claude/x
    # moves; openai/y stays put (not in the apply scope).
    body = "---\ntitle: Shared\n---\n# body\n"
    for coll, slug in [("claude", "x"), ("openai", "y")]:
        page = wiki.wiki_dir / coll / f"{slug}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(body, encoding="utf-8")
    _git_init(wiki.data_root)

    result = runner.invoke(
        app,
        [
            "ingest-to-library",
            "--collection",
            "claude",
            "--apply",
            "--name",
            name,
        ],
        env={"XDG_DATA_HOME": str(tmp_path), "LIES_WIKI_NAME": name},
    )
    assert result.exit_code == 0, (
        f"exit={result.exit_code}; stdout={result.stdout!r}; stderr={result.stderr!r}"
    )

    # claude/x moved; openai/y was filtered out (stays in wiki).
    assert (lib.collections_root / "claude" / "x.md").exists()
    assert not (lib.collections_root / "openai" / "y.md").exists()
    assert (wiki.wiki_dir / "openai" / "y.md").exists(), (
        "openai/y should stay in wiki when --collection=claude filters it out"
    )
    # Catalog only got the claude/x row.
    conn = open_catalog(lib)
    try:
        rows = list_pages(conn)
    finally:
        conn.close()
    assert {r.slug for r in rows} == {"claude/x"}
