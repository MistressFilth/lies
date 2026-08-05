"""Tests for lies migrate-xdg."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lies.migrate import migrate_wiki


def test_migrate_copies_state(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/legacy-wiki")
    legacy = tmp_path / "legacy"
    shutil.copytree(fixture, legacy)
    xdg_data = tmp_path / "data"
    xdg_config = tmp_path / "config"
    xdg_cache = tmp_path / "cache"
    xdg_state = tmp_path / "state"

    result = migrate_wiki(
        legacy,
        name="migrated",
        xdg_data_home=xdg_data,
        xdg_config_home=xdg_config,
        xdg_cache_home=xdg_cache,
        xdg_state_home=xdg_state,
    )
    assert result.removed_legacy
    assert (xdg_config / "lies" / "migrated" / "schema.md").exists()
    assert (xdg_config / "lies" / "migrated" / "collections" / "foo.yaml").exists()
    assert (xdg_cache / "lies" / "migrated" / "hashes" / "foo.json").exists()
    assert (xdg_cache / "lies" / "migrated" / "collections" / "foo" / "manifest.json").exists()
    assert (xdg_state / "lies" / "migrated" / "logs" / "foo.log").exists()
    assert (xdg_state / "lies" / "migrated" / "mcp.log").exists()
    assert not (legacy / ".lies").exists()
    assert (legacy / ".xdg-migrated").exists()


def test_migrate_idempotent(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/legacy-wiki")
    legacy = tmp_path / "legacy"
    shutil.copytree(fixture, legacy)
    kwargs = {
        "name": "migrated",
        "xdg_data_home": tmp_path / "data",
        "xdg_config_home": tmp_path / "config",
        "xdg_cache_home": tmp_path / "cache",
        "xdg_state_home": tmp_path / "state",
    }
    migrate_wiki(legacy, **kwargs)
    result = migrate_wiki(legacy, **kwargs)
    assert not result.removed_legacy  # already migrated


def test_migrate_refuses_on_conflict(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/legacy-wiki")
    legacy = tmp_path / "legacy"
    shutil.copytree(fixture, legacy)
    xdg_config = tmp_path / "config"
    target = xdg_config / "lies" / "migrated" / "schema.md"
    target.parent.mkdir(parents=True)
    target.write_text("DIFFERENT")
    with pytest.raises(Exception) as exc:
        migrate_wiki(
            legacy,
            name="migrated",
            xdg_data_home=tmp_path / "data",
            xdg_config_home=xdg_config,
            xdg_cache_home=tmp_path / "cache",
            xdg_state_home=tmp_path / "state",
        )
    assert "conflict" in str(exc.value).lower() or "schema.md" in str(exc.value)
