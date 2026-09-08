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

from lies.library.catalog import LibraryCatalogPage
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


def apply_migration(plan: MigrationPlan, *, dry_run: bool = True) -> None:
    if dry_run:
        return
    for src, dst in plan.moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        rewritten = _rewrite_frontmatter(src)
        dst.write_text(rewritten, encoding="utf-8")
        # Atomic commit at library side handled by LibraryWriter in Task 13,
        # but the wiki-side move also needs atomic git commit. Left to the
        # full Task 14 (migrate CLI) which wraps both envelopes.
    for src, backup in plan.duplicates_to_backup:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, backup)


def _rewrite_frontmatter(src: Path) -> str:
    """Re-tag wiki-side frontmatter to library schema.

    - Drops ``type:`` (library mirrors are type-less).
    - Adds ``source_url: null``, ``source_path: <absolute>``,
      ``source_hash: <sha>``, ``fetched_via: migration``,
      ``ingested_at: <date derived from hash>``.
    - Preserves ``title:``.
    """
    body = src.read_text(encoding="utf-8")
    post = frontmatter.loads(body)
    title = str(post.get("title", "") or src.stem.replace("-", " ").title())
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    fm = build_frontmatter(
        title=title,
        source_url=None,
        source_path=str(src.resolve()),
        source_hash=sha,
        fetched_via="migration",
    )
    md = post.content if post.content else ""
    return fm + "\n" + md if not md.endswith("\n") else fm + "\n" + md


__all__ = ("MigrationPlan", "plan_migration", "apply_migration")
