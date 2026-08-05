"""Migrate a legacy ``.lies/`` wiki into XDG role-routed dirs."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from lies import xdg
from lies.errors import WikiAlreadyExists
from lies.wiki.validation import validate_name


@dataclass
class MigrationResult:
    copied: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    conflicts: list[tuple[Path, Path]] = field(default_factory=list)
    removed_legacy: bool = False


class MigrationConflict(Exception):
    def __init__(self, conflicts: list[tuple[Path, Path]]) -> None:
        self.conflicts = conflicts
        super().__init__(
            f"migration aborted; {len(conflicts)} conflict(s); "
            "resolve manually or rerun with --force."
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_or_skip_or_conflict(src: Path, dst: Path, result: MigrationResult) -> None:
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        result.copied.append(dst)
        return
    if _sha256(src) == _sha256(dst):
        result.skipped.append(dst)
        return
    result.conflicts.append((src, dst))


def migrate_wiki(
    legacy_path: Path,
    *,
    name: str,
    xdg_data_home: Path | None = None,
    xdg_config_home: Path | None = None,
    xdg_cache_home: Path | None = None,
    xdg_state_home: Path | None = None,
    force: bool = False,
) -> MigrationResult:
    """Migrate ``<legacy_path>/.lies/`` into role-routed XDG dirs under ``name``."""
    validate_name(name)
    legacy_lies = legacy_path / ".lies"
    marker = legacy_path / ".xdg-migrated"
    if marker.exists():
        return MigrationResult()  # idempotent no-op

    dh = xdg_data_home or xdg.data_home()
    ch = xdg_config_home or xdg.config_home()
    cah = xdg_cache_home or xdg.cache_home()
    sh = xdg_state_home or xdg.state_home()

    config_root = ch / "lies" / name
    cache_root = cah / "lies" / name
    state_root = sh / "lies" / name
    data_root = dh / "lies" / name

    if not legacy_lies.exists():
        return MigrationResult()

    if data_root.exists():
        raise WikiAlreadyExists(name, data_root)

    result = MigrationResult()

    # 1. schema.md
    src = legacy_lies / "schema.md"
    if src.exists():
        _copy_or_skip_or_conflict(src, config_root / "schema.md", result)

    # 2. collections/*.yaml
    legacy_collections = legacy_lies / "collections"
    if legacy_collections.exists():
        for yaml in legacy_collections.glob("*.yaml"):
            _copy_or_skip_or_conflict(yaml, config_root / "collections" / yaml.name, result)

    # 3. hashes/*.json
    legacy_hashes = legacy_lies / "hashes"
    if legacy_hashes.exists():
        for h in legacy_hashes.glob("*.json"):
            _copy_or_skip_or_conflict(h, cache_root / "hashes" / h.name, result)

    # 4. collections/*/manifest.json
    if legacy_collections.exists():
        for sub in legacy_collections.iterdir():
            if sub.is_dir():
                m = sub / "manifest.json"
                if m.exists():
                    _copy_or_skip_or_conflict(
                        m,
                        cache_root / "collections" / sub.name / "manifest.json",
                        result,
                    )

    # 5. mcp.log
    src = legacy_lies / "mcp.log"
    if src.exists():
        _copy_or_skip_or_conflict(src, state_root / "mcp.log", result)

    # 6. logs/*.log
    legacy_logs = legacy_lies / "logs"
    if legacy_logs.exists():
        for log in legacy_logs.glob("*.log"):
            _copy_or_skip_or_conflict(log, state_root / "logs" / log.name, result)

    # 7. poison/<c>/*
    legacy_poison = legacy_lies / "poison"
    if legacy_poison.exists():
        for coll in legacy_poison.iterdir():
            if coll.is_dir():
                target = state_root / "poison" / coll.name
                target.mkdir(parents=True, exist_ok=True)
                for entry in coll.iterdir():
                    _copy_or_skip_or_conflict(entry, target / entry.name, result)

    if result.conflicts and not force:
        raise MigrationConflict(result.conflicts)

    # 8. Remove legacy .lies/, write marker.
    shutil.rmtree(legacy_lies)
    marker.write_text(f"migrated to {data_root}\n", encoding="utf-8")
    result.removed_legacy = True
    return result
