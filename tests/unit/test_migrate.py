"""Tests for lies migrate-xdg."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from lies.migrate import MigrationConflict, migrate_wiki
from lies.wiki.wiki import Wiki


def _copy_legacy(tmp_path: Path) -> Path:
    """Copy the legacy fixture into ``tmp_path/legacy`` and return that path."""
    fixture = Path("tests/fixtures/legacy-wiki")
    legacy = tmp_path / "legacy"
    shutil.copytree(fixture, legacy)
    return legacy


def _xdg_kwargs(tmp_path: Path) -> dict[str, Path]:
    """Override kwargs aligned with the autouse ``_isolated_xdg`` redirect.

    The conftest's autouse fixture points ``XDG_*`` env vars at
    ``tmp_path/xdg/<role>/``; we use the same paths as ``xdg_*_home`` overrides
    so ``Wiki.require`` after migration can find the migrated wiki.
    """
    xdg = tmp_path / "xdg"
    return {
        "xdg_data_home": xdg / "data",
        "xdg_config_home": xdg / "config",
        "xdg_cache_home": xdg / "cache",
        "xdg_state_home": xdg / "state",
    }


def test_migrate_copies_state(tmp_path: Path) -> None:
    legacy = _copy_legacy(tmp_path)
    kwargs = _xdg_kwargs(tmp_path)

    result = migrate_wiki(legacy, name="migrated", **kwargs)
    assert result.removed_legacy
    assert (kwargs["xdg_config_home"] / "lies" / "migrated" / "schema.md").exists()
    assert (kwargs["xdg_config_home"] / "lies" / "migrated" / "collections" / "foo.yaml").exists()
    assert (kwargs["xdg_cache_home"] / "lies" / "migrated" / "hashes" / "foo.json").exists()
    assert (
        kwargs["xdg_cache_home"] / "lies" / "migrated" / "collections" / "foo" / "manifest.json"
    ).exists()
    assert (kwargs["xdg_state_home"] / "lies" / "migrated" / "logs" / "foo.log").exists()
    assert (kwargs["xdg_state_home"] / "lies" / "migrated" / "mcp.log").exists()
    assert not (legacy / ".lies").exists()
    assert (legacy / ".xdg-migrated").exists()


def test_migrate_idempotent(tmp_path: Path) -> None:
    legacy = _copy_legacy(tmp_path)
    kwargs = _xdg_kwargs(tmp_path)
    migrate_wiki(legacy, name="migrated", **kwargs)
    result = migrate_wiki(legacy, name="migrated", **kwargs)
    assert not result.removed_legacy  # already migrated


def test_migrate_refuses_on_conflict(tmp_path: Path) -> None:
    legacy = _copy_legacy(tmp_path)
    kwargs = _xdg_kwargs(tmp_path)
    target = kwargs["xdg_config_home"] / "lies" / "migrated" / "schema.md"
    target.parent.mkdir(parents=True)
    target.write_text("DIFFERENT")
    with pytest.raises(MigrationConflict) as exc:
        migrate_wiki(legacy, name="migrated", **kwargs)
    assert "conflict" in str(exc.value).lower()
    assert exc.value.conflicts  # populated so caller can see what to fix
    # Legacy .lies/ must NOT be removed when conflicts abort the migration.
    assert (legacy / ".lies").exists()


def test_migrate_creates_usable_wiki(tmp_path: Path) -> None:
    """After migration, ``Wiki.require(name)`` succeeds with content + .git."""
    legacy = _copy_legacy(tmp_path)
    kwargs = _xdg_kwargs(tmp_path)

    result = migrate_wiki(legacy, name="migrated", **kwargs)
    assert result.removed_legacy

    wiki = Wiki.require("migrated")
    assert (wiki.data_root / "raw" / "article1.md").exists()
    assert (wiki.data_root / "wiki" / "index.md").exists()
    assert (wiki.data_root / "wiki" / "log.md").exists()
    assert (wiki.data_root / ".git").exists()
    # Role roots created.
    assert wiki.config_root.is_dir()
    assert wiki.cache_root.is_dir()
    assert wiki.state_root.is_dir()
    assert wiki.runtime_root.is_dir()


def test_migrate_initializes_git_with_commit(tmp_path: Path) -> None:
    """No legacy .git/ -> fresh ``git init`` with an initial commit."""
    legacy = _copy_legacy(tmp_path)
    kwargs = _xdg_kwargs(tmp_path)

    migrate_wiki(legacy, name="migrated", **kwargs)

    data_root = kwargs["xdg_data_home"] / "lies" / "migrated"
    log = subprocess.run(
        ["git", "-C", str(data_root), "log", "--oneline"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "initial wiki" in log.stdout


def test_migrate_refuses_when_destination_exists(tmp_path: Path) -> None:
    """If ``lies init <name>`` already created the destination, refuse."""
    from lies.errors import WikiAlreadyExists

    legacy = _copy_legacy(tmp_path)
    kwargs = _xdg_kwargs(tmp_path)
    # Pre-create the destination data_root as if ``lies init`` ran first.
    (kwargs["xdg_data_home"] / "lies" / "migrated").mkdir(parents=True)
    with pytest.raises(WikiAlreadyExists):
        migrate_wiki(legacy, name="migrated", **kwargs)
    # Legacy state is untouched.
    assert (legacy / ".lies").exists()


def test_migrate_noop_when_lies_dir_missing(tmp_path: Path) -> None:
    """Legacy path is a directory but has no ``.lies/`` -> no-op success."""
    empty_legacy = tmp_path / "empty-legacy"
    empty_legacy.mkdir()
    result = migrate_wiki(empty_legacy, name="migrated", **_xdg_kwargs(tmp_path))
    assert not result.removed_legacy
    assert not result.copied
