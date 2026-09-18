"""End-to-end integration tests for the ``lies migrate-collection-configs`` CLI.

Exercises the CLI surface (``dry-run``, ``apply``, exit codes, idempotency,
``--force`` overwrite of a library-side collision) — the unit suite covers
the per-function helpers; this file pins the operator-facing contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.library.config_io import load_config, save_config
from lies.library.paths import Library
from lies.library.record import LibraryCollectionConfig


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Library:
    lib = Library(
        collections_root=tmp_path / "library" / "collections",
        catalog_path=tmp_path / "library" / ".lies" / "catalog.db",
        log_path=tmp_path / "library" / "log.md",
        poison_root=tmp_path / "library" / "poison",
        git_root=tmp_path / "library",
    )
    monkeypatch.setattr(Library, "open", classmethod(lambda cls: lib))
    lib.collections_root.mkdir(parents=True)
    return lib


@pytest.fixture
def wikis_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the XDG config root at a private ``wikis_root``."""
    root = tmp_path / "config" / "lies"
    root.mkdir(parents=True)
    monkeypatch.setattr(xdg, "config_home", lambda: tmp_path / "config")
    return root


def _seed_wiki_yaml(path: Path, name: str = "claude_code") -> None:
    rec = LibraryCollectionConfig(
        name=name,
        source="https://example.com/llms.txt",
        tags=("skills",),
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=datetime(2026, 9, 13, tzinfo=UTC),
        updated_at=datetime(2026, 9, 13, tzinfo=UTC),
        config={},
    )
    payload = {
        "name": rec.name,
        "source": rec.source,
        "tags": list(rec.tags),
        "scraper_cmd": rec.scraper_cmd,
        "doc_path": str(rec.doc_path) if rec.doc_path else None,
        "mapper_model": rec.mapper_model,
        "language": rec.language,
        "version": rec.version,
        "created_at": rec.created_at.isoformat(),
        "updated_at": rec.updated_at.isoformat(),
        "config": rec.config,
        "path": "/tmp/legacy/raw/" + name,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=True))


def test_cli_dry_run_prints_moves_without_writing(library: Library, wikis_root: Path) -> None:
    wiki_dir = wikis_root / "default"
    wiki_dir.mkdir()
    src = wiki_dir / "collections" / "claude_code.yaml"
    _seed_wiki_yaml(src)

    runner = CliRunner()
    result = runner.invoke(app, ["migrate-collection-configs", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "claude_code" in result.output
    assert src.exists(), "dry-run must not delete the source"
    assert not (library.collections_root / "claude_code" / "config.yaml").exists()


def test_cli_apply_relocates_yaml_to_library(library: Library, wikis_root: Path) -> None:
    wiki_dir = wikis_root / "default"
    wiki_dir.mkdir()
    src = wiki_dir / "collections" / "claude_code.yaml"
    _seed_wiki_yaml(src)

    runner = CliRunner()
    result = runner.invoke(app, ["migrate-collection-configs", "--apply"])

    assert result.exit_code == 0, result.output
    assert not src.exists()
    loaded = load_config("claude_code")
    assert loaded.source == "https://example.com/llms.txt"
    assert loaded.tags == ("skills",)


def test_cli_apply_is_idempotent(library: Library, wikis_root: Path) -> None:
    wiki_dir = wikis_root / "default"
    wiki_dir.mkdir()
    _seed_wiki_yaml(wiki_dir / "collections" / "claude_code.yaml")

    runner = CliRunner()
    first = runner.invoke(app, ["migrate-collection-configs", "--apply"])
    second = runner.invoke(app, ["migrate-collection-configs", "--apply"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    # Library config still holds the migrated record.
    assert load_config("claude_code").source == "https://example.com/llms.txt"


def test_cli_duplicate_slug_exits_2(library: Library, wikis_root: Path) -> None:
    a = wikis_root / "wiki_a"
    b = wikis_root / "wiki_b"
    a.mkdir()
    b.mkdir()
    _seed_wiki_yaml(a / "collections" / "llms.yaml", name="llms")
    _seed_wiki_yaml(b / "collections" / "llms.yaml", name="llms")

    runner = CliRunner()
    result = runner.invoke(app, ["migrate-collection-configs", "--apply"])

    assert result.exit_code == 2, result.output
    # Both source YAMLs are still on disk (apply aborted pre-write).
    assert (a / "collections" / "llms.yaml").exists()
    assert (b / "collections" / "llms.yaml").exists()


def test_cli_apply_with_force_overwrites_library_collision(
    library: Library, wikis_root: Path
) -> None:
    """Partial-state re-run with --force: the library config gets overwritten."""
    wiki_dir = wikis_root / "default"
    wiki_dir.mkdir()
    src = wiki_dir / "collections" / "claude_code.yaml"
    _seed_wiki_yaml(src)
    # Seed the library with the same slug, different source.
    save_config(
        LibraryCollectionConfig(
            name="claude_code",
            source="https://other.example.com/llms.txt",
            tags=(),
            scraper_cmd=None,
            doc_path=None,
            mapper_model=None,
            language=None,
            version="1",
            created_at=datetime(2026, 9, 13, tzinfo=UTC),
            updated_at=datetime(2026, 9, 13, tzinfo=UTC),
            config={},
        )
    )

    runner = CliRunner()
    # Without --force, aborts.
    blocked = runner.invoke(app, ["migrate-collection-configs", "--apply"])
    assert blocked.exit_code == 2, blocked.output
    # With --force, overwrites.
    applied = runner.invoke(app, ["migrate-collection-configs", "--apply", "--force"])
    assert applied.exit_code == 0, applied.output
    assert load_config("claude_code").source == "https://example.com/llms.txt"
    assert not src.exists()
