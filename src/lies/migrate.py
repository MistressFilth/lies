"""Migrate a legacy ``.lies/`` wiki into XDG role-routed dirs."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from lies import xdg
from lies.errors import WikiAlreadyExists
from lies.wiki.layout import git_init_initial
from lies.wiki.validation import validate_name
from lies.wiki.wiki import Wiki


@dataclass
class MigrationResult:
    copied: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    conflicts: list[tuple[Path, Path]] = field(default_factory=list)
    quarantined: list[tuple[Path, Path]] = field(default_factory=list)
    removed_legacy: bool = False


class MigrationConflict(Exception):
    def __init__(self, conflicts: list[tuple[Path, Path]]) -> None:
        self.conflicts = conflicts
        super().__init__(
            f"migration aborted; {len(conflicts)} conflict(s); "
            "resolve manually or rerun with --force to quarantine."
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


def _quarantine_conflict(
    src: Path, legacy_lies: Path, backup_root: Path, result: MigrationResult
) -> None:
    """Copy a conflicting ``src`` into ``<backup_root>/<rel>``.

    ``backup_root`` is the directory that will survive the rmtree of
    ``legacy_lies``; the caller picks it. The relative path under
    ``legacy_lies`` is preserved so the caller can recover the original
    by reading ``<backup_root>/<rel>`` after the migration completes.
    The destination file at the wiki root is left untouched (the legacy
    destination wins).
    """
    rel = src.relative_to(legacy_lies)
    backup = backup_root / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, backup)
    result.quarantined.append((src, backup))


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
    """Migrate ``<legacy_path>/.lies/`` into role-routed XDG dirs under ``name``.

    After migration, ``Wiki.require(name)`` succeeds and the wiki's
    ``data_root`` contains ``raw/``, ``wiki/``, and ``.git/`` populated from
    the legacy path. Git history is preserved when ``<legacy_path>/.git/``
    exists; a fresh repo is initialised otherwise.

    Conflict semantics: a *conflict* is a destination file that already
    exists and whose bytes do not match the source. By default, a single
    conflict aborts the migration so the caller can resolve it. With
    ``force=True``, the legacy source is *quarantined* — copied to
    ``<legacy_path>/.xdg-migration-conflicts/<rel>`` so the original
    bytes are recoverable after the legacy ``.lies/`` directory is
    removed. The destination file is left untouched.

    Raises:
        NotADirectoryError: ``legacy_path`` exists but is not a directory.
        WikiAlreadyExists: A wiki with this name already lives at the
            destination ``data_root`` (e.g. ``lies init <name>`` ran first).
        MigrationConflict: Destination files exist with byte-mismatched
            content and ``force=False``.
    """
    validate_name(name)
    if legacy_path.exists() and not legacy_path.is_dir():
        raise NotADirectoryError(f"legacy_path must be a directory, got file: {legacy_path}")
    legacy_lies = legacy_path / ".lies"
    marker = legacy_path / ".xdg-migrated"
    if marker.exists():
        return MigrationResult()  # idempotent no-op

    dh = xdg_data_home or xdg.data_home()
    ch = xdg_config_home or xdg.config_home()
    cah = xdg_cache_home or xdg.cache_home()
    sh = xdg_state_home or xdg.state_home()

    wiki = Wiki(
        name=name,
        data_root=dh / "lies" / name,
        config_root=ch / "lies" / name,
        cache_root=cah / "lies" / name,
        state_root=sh / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )

    if wiki.data_root.exists():
        raise WikiAlreadyExists(name, wiki.data_root)

    if not legacy_lies.exists():
        return MigrationResult()

    result = MigrationResult()

    # 1. Create all 5 role roots so Wiki.require(name) succeeds post-migration.
    for root in (
        wiki.data_root,
        wiki.config_root,
        wiki.cache_root,
        wiki.state_root,
        wiki.runtime_root,
    ):
        root.mkdir(parents=True, exist_ok=True)

    # 2. Copy raw/ and wiki/ from legacy into data_root.
    legacy_raw = legacy_path / "raw"
    legacy_wiki = legacy_path / "wiki"
    if legacy_raw.is_dir():
        shutil.copytree(legacy_raw, wiki.raw_dir, dirs_exist_ok=True)
    else:
        wiki.raw_dir.mkdir(exist_ok=True)
    if legacy_wiki.is_dir():
        shutil.copytree(legacy_wiki, wiki.wiki_dir, dirs_exist_ok=True)
    else:
        wiki.wiki_dir.mkdir(exist_ok=True)

    # 3. Preserve git history when legacy .git/ exists; otherwise init fresh.
    legacy_git = legacy_path / ".git"
    if legacy_git.is_dir():
        shutil.copytree(legacy_git, wiki.data_root / ".git", symlinks=True)
    else:
        git_init_initial(wiki.data_root)

    # 4. Migrate .lies/ contents to role-routed roots.
    # 4a. schema.md -> config_root
    src = legacy_lies / "schema.md"
    if src.exists():
        _copy_or_skip_or_conflict(src, wiki.schema_path, result)

    # 4b. collections/*.yaml -> config_root/collections/
    legacy_collections = legacy_lies / "collections"
    if legacy_collections.is_dir():
        for yaml in legacy_collections.glob("*.yaml"):
            _copy_or_skip_or_conflict(yaml, wiki.collections_dir / yaml.name, result)

    # 4c. hashes/*.json -> cache_root/hashes/
    legacy_hashes = legacy_lies / "hashes"
    if legacy_hashes.is_dir():
        for h in legacy_hashes.glob("*.json"):
            _copy_or_skip_or_conflict(h, wiki.hashes_dir / h.name, result)

    # 4d. collections/<c>/manifest.json -> cache_root/collections/<c>/
    if legacy_collections.is_dir():
        for sub in legacy_collections.iterdir():
            if sub.is_dir():
                m = sub / "manifest.json"
                if m.exists():
                    _copy_or_skip_or_conflict(
                        m,
                        wiki.cache_root / "collections" / sub.name / "manifest.json",
                        result,
                    )

    # 4e. mcp.log -> state_root
    src = legacy_lies / "mcp.log"
    if src.exists():
        _copy_or_skip_or_conflict(src, wiki.mcp_log_path, result)

    # 4f. logs/*.log -> state_root/logs/
    legacy_logs = legacy_lies / "logs"
    if legacy_logs.is_dir():
        for log in legacy_logs.glob("*.log"):
            _copy_or_skip_or_conflict(log, wiki.logs_dir / log.name, result)

    # 4g. poison/<c>/* -> state_root/poison/<c>/
    legacy_poison = legacy_lies / "poison"
    if legacy_poison.is_dir():
        for coll in legacy_poison.iterdir():
            if coll.is_dir():
                target = wiki.poison_root / coll.name
                target.mkdir(parents=True, exist_ok=True)
                for entry in coll.iterdir():
                    _copy_or_skip_or_conflict(entry, target / entry.name, result)

    # 4h. locks + pid -> runtime_root
    legacy_locks = [
        ("memory.lock", wiki.memory_lock_path),
        ("sync.lock", wiki.sync_lock_path),
        ("sync.lock.create", wiki.sync_create_lock_path),
        ("sync.lock.fd", wiki.sync_fd_path),
        ("mcp.pid", wiki.mcp_pid_path),
        ("mcp.pid.create", wiki.mcp_create_lock_path),
    ]
    for legacy_name, dest in legacy_locks:
        src = legacy_lies / legacy_name
        if src.exists():
            _copy_or_skip_or_conflict(src, dest, result)

    if result.conflicts and not force:
        raise MigrationConflict(result.conflicts)

    # 5. With --force, quarantine conflicting source files before removing
    # the legacy ``.lies/`` directory so the original bytes remain
    # recoverable from ``<legacy_path>/.xdg-migration-conflicts/``.
    if result.conflicts and force:
        backup_root = legacy_path / ".xdg-migration-conflicts"
        for src, _dst in result.conflicts:
            _quarantine_conflict(src, legacy_lies, backup_root, result)

    # 6. Remove legacy .lies/, write marker.
    shutil.rmtree(legacy_lies)
    marker.write_text(f"migrated to {wiki.data_root}\n", encoding="utf-8")
    result.removed_legacy = True
    return result
