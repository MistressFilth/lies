"""One-shot migration: move wiki-resident ingests to library.

Per consolidate-wikis resolved Q #6 (2026-09-02):

- Live content moves to library.
- Byte-identical duplicates across collections move to
  ``<wiki>/.lies/migration-backup/<date>/<collection>/<slug>.md``.
- Wiki catalog rows get ``section="library-migrated"`` (filtered
  out of the wiki-side qmd index).
- Library catalog rows are inserted with deterministic ``updated``.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

from lies.library.catalog import (
    LibraryCatalogPage,
    open_catalog,
    upsert_pages,
)
from lies.library.frontmatter import _ingested_at_from_hash, build_frontmatter
from lies.library.paths import Library
from lies.wiki.wiki import Wiki


@dataclass(frozen=True)
class MigrationPlan:
    moves: list[tuple[Path, Path]] = field(default_factory=list)
    duplicates_to_backup: list[tuple[Path, Path]] = field(default_factory=list)
    catalog_updates: list[LibraryCatalogPage] = field(default_factory=list)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_migration(wiki: Wiki, library: Library, *, date_str: str = "today") -> MigrationPlan:
    plan = MigrationPlan()
    wiki_pages_dir = wiki.wiki_dir
    seen_hashes: dict[str, Path] = {}
    if not wiki_pages_dir.exists():
        return plan
    for md in sorted(wiki_pages_dir.rglob("*.md")):
        rel = md.relative_to(wiki_pages_dir)
        if rel.parts[0] in {"index.md", "log.md", "schema.md"}:
            continue
        if len(rel.parts) < 2:
            continue
        coll_name, slug = rel.parts[0], md.stem
        sha = _hash_file(md)
        dst = library.collections_root / coll_name / f"{slug}.md"
        if sha in seen_hashes:
            backup = (
                wiki.wiki_dir.parent
                / ".lies"
                / "migration-backup"
                / date_str
                / coll_name
                / f"{slug}.md"
            )
            plan.duplicates_to_backup.append((md, backup))
            continue
        seen_hashes[sha] = md
        plan.moves.append((md, dst))
        plan.catalog_updates.append(
            LibraryCatalogPage(
                slug=f"{coll_name}/{slug}",
                title=slug.replace("-", " ").title(),
                type="",
                source_pkg=coll_name,
                section="library",
                updated=_deterministic_updated(sha),
                hash=sha,
                derived_from="",
            )
        )
    return plan


def _deterministic_updated(source_hash: str) -> str:
    """Match the frontmatter ingested_at scheme: 4-byte-mod-days since 2024."""
    return f"{_ingested_at_from_hash(source_hash)}T00:00:00+00:00"


def apply_migration(
    plan: MigrationPlan,
    library: Library,
    *,
    dry_run: bool = True,
    commit_message: str | None = None,
) -> str | None:
    """Apply a migration plan in three stages.

    1. Move wiki pages to ``library/collections/<coll>/<slug>.md``.
    2. Backup byte-identical duplicates at
       ``<wiki>/.lies/migration-backup/<date>/<coll>/<slug>.md`` AND
       remove the duplicate wiki file (``src.unlink()``) so the wiki
       atomic-commit picks up the deletion in the same commit. The
       first-seen wins (per consolidate-wikis resolved Q #6).
    3. Upsert library catalog rows with section="library" + a
       deterministic ``updated`` derived from the source hash (per
       spec §Migration §catalog state).

    Returns the library-side git commit SHA (``None`` on no-op), routed
    through ``LibraryWriter.commit`` so the wiki side and library side
    each get their own atomic-commit envelope (per spec §Migration
    §Atomicity: "Per-collection library write = one atomic git commit
    at the library").
    """
    if dry_run:
        return None
    moved_paths: list[Path] = []
    for src, dst in plan.moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        rewritten = _rewrite_frontmatter(src)
        dst.write_text(rewritten, encoding="utf-8")
        moved_paths.append(dst)
    for src, backup in plan.duplicates_to_backup:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, backup)
        # C3: remove the duplicate wiki file so the wiki-side
        # atomic-commit picks up the removal. The backup stays as a
        # workspace file (per spec §Duplicates).
        src.unlink()
    # Catalog upsert: library catalog rows inserted with section="library"
    # and deterministic `updated` (per spec §Migration §catalog state).
    # ``upsert_pages`` commits internally (matches the wiki-side contract).
    # WAL + busy_timeout protect against concurrent writers.
    if plan.catalog_updates:
        conn = open_catalog(library)
        try:
            upsert_pages(conn, plan.catalog_updates)
        finally:
            conn.close()

    # C2: library-side atomic commit, routed through LibraryWriter so
    # the catalog upsert + the mirror writes land in the same envelope
    # as the rest of the library write path. No-op when nothing moved
    # (matches PR #38 contract).
    if moved_paths or plan.catalog_updates:
        from lies.library.writer import LibraryWriter

        writer = LibraryWriter(library)
        rel_paths = [p.relative_to(library.git_root) for p in moved_paths]
        msg = commit_message or f"migrate: ingest-to-library +{len(moved_paths)}"
        return writer.commit(
            rel_paths,
            message=msg,
            catalog_updates=plan.catalog_updates,
        )
    return None


def _rewrite_frontmatter(src: Path) -> str:
    """Re-tag wiki-side frontmatter to library schema.

    - Drops ``type:`` (library mirrors are type-less).
    - Adds ``source_url: null``, ``source_path: "migrated-from-wiki"``
      (stable placeholder — see I9), ``source_hash: <sha>``,
      ``fetched_via: migration``, ``ingested_at: <date derived from hash>``.
    - Preserves ``title:``.

    ``source_path`` is set to a stable placeholder rather than the
    host-local ``str(src.resolve())`` so migrated mirror files are
    host-independent: relocating the wiki directory doesn't change
    the byte-identical determinism contract on the library side.
    """
    body = src.read_text(encoding="utf-8")
    post = frontmatter.loads(body)
    title = str(post.get("title", "") or src.stem.replace("-", " ").title())
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    fm = build_frontmatter(
        title=title,
        source_url=None,
        source_path="migrated-from-wiki",
        source_hash=sha,
        fetched_via="migration",
    )
    md = post.content if post.content else ""
    return fm + "\n" + md if not md.endswith("\n") else fm + "\n" + md


__all__ = ("MigrationPlan", "plan_migration", "apply_migration")
