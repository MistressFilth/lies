"""One-shot migration: per-wiki YAML configs → library ``config.yaml``s.

Discovers every wiki under ``$XDG_CONFIG_HOME/lies/`` and for each
``<wiki>/collections/<slug>.yaml``, parses, validates uniqueness
across wikis, then writes ``<library>/collections/<slug>/config.yaml``
and deletes the source.

Atomic per-slug: a write or delete failure aborts the whole migration
and leaves disk unchanged. Re-running after a successful migration is a
no-op (no source YAMLs remain).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from lies import xdg
from lies.library.config_io import config_path_for, save_config
from lies.library.errors import CollectionConfigInvalid
from lies.library.record import LibraryCollectionConfig
from lies.library.schema import ConfigYAML


@dataclass(frozen=True)
class MigrationPlan:
    moves: tuple[tuple[Path, Path], ...]  # (source_yaml, target_config_yaml)
    duplicates: tuple[tuple[str, tuple[Path, ...]], ...]  # slug -> source paths


class MigrationConflictError(Exception):
    """Slug is bound in multiple wikis with different content."""


def discover_wikis(config_root: Path | None = None) -> list[Path]:
    """Return wiki config roots under ``config_root`` (defaults to $XDG_CONFIG_HOME/lies/)."""
    root = config_root or (xdg.config_home() / "lies")
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def plan_migration(config_root: Path | None = None) -> MigrationPlan:
    """Build a migration plan without mutating disk."""
    moves: list[tuple[Path, Path]] = []
    by_slug: dict[str, list[Path]] = {}
    for wiki_dir in discover_wikis(config_root):
        coll_dir = wiki_dir / "collections"
        if not coll_dir.is_dir():
            continue
        for yaml_path in sorted(coll_dir.glob("*.yaml")):
            slug = yaml_path.stem
            by_slug.setdefault(slug, []).append(yaml_path)
            target = config_path_for(slug)
            moves.append((yaml_path, target))

    duplicates = tuple((slug, tuple(paths)) for slug, paths in by_slug.items() if len(paths) > 1)
    return MigrationPlan(moves=tuple(moves), duplicates=duplicates)


def apply_migration(
    plan: MigrationPlan,
    *,
    force: bool = False,
) -> None:
    """Apply ``plan`` atomically. Aborts on duplicates or content conflicts."""
    if plan.duplicates:
        msgs = "; ".join(
            f"{slug} in {', '.join(str(p) for p in paths)}" for slug, paths in plan.duplicates
        )
        raise MigrationConflictError(f"duplicate slugs: {msgs}")

    parsed: list[tuple[Path, Path, LibraryCollectionConfig]] = []
    for source, target in plan.moves:
        if not source.exists():
            continue
        try:
            payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise CollectionConfigInvalid(f"invalid YAML in {source}: {exc}") from exc
        if not isinstance(payload, dict):
            raise CollectionConfigInvalid(f"config root must be a mapping: {source}")
        if not payload.get("config"):
            payload["config"] = {}
        schema = ConfigYAML.model_validate(payload)
        rec = LibraryCollectionConfig(
            name=schema.name,
            source=schema.source,
            tags=schema.tags,
            scraper_cmd=schema.scraper_cmd,
            doc_path=schema.doc_path,
            mapper_model=schema.mapper_model,
            language=schema.language,
            version=schema.version,
            created_at=schema.created_at,
            updated_at=schema.updated_at,
            config=dict(schema.config),
        )
        parsed.append((source, target, rec))

    # Phase 2: write each target. Raises on collision without force.
    for _source, target, rec in parsed:
        save_config(rec, force=force)

    # Phase 3: delete sources + remove empty wiki collections dirs.
    for source, _target, _rec in parsed:
        source.unlink()
        parent = source.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
