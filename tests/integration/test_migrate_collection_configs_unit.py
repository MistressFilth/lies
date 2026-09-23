from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.library.config_io import load_config
from lies.library.migrate_collection_configs import (
    MigrationConflictError,
    plan_migration,
    apply_migration,
)
from lies.library.paths import Library


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = Library(
        collections_root=tmp_path / "collections",
        catalog_path=tmp_path / ".lies" / "catalog.db",
        log_path=tmp_path / "log.md",
        poison_root=tmp_path / "poison",
        git_root=tmp_path,
    )
    monkeypatch.setattr(Library, "open", classmethod(lambda cls: lib))
    (tmp_path / "collections").mkdir(parents=True)
    return lib


@pytest.fixture
def wikis_root(tmp_path, monkeypatch):
    from lies import xdg

    root = tmp_path / "config" / "lies"
    root.mkdir(parents=True)
    monkeypatch.setattr(xdg, "config_home", lambda: tmp_path / "config")
    return root


def _write_yaml(path: Path, name: str = "claude_code") -> None:
    from lies.library.record import LibraryCollectionConfig
    import yaml

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
    path.parent.mkdir(parents=True, exist_ok=True)
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
        # Legacy `path` field; must be dropped by the migrator.
        "path": "/home/divinefilth/.local/share/lies/default/raw/" + name,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=True))


def test_migration_copies_yaml_to_library(library: Library, wikis_root: Path) -> None:
    wiki_dir = wikis_root / "default"
    wiki_dir.mkdir()
    _write_yaml(wiki_dir / "collections" / "claude_code.yaml")
    apply_migration(plan_migration(wikis_root))
    assert load_config("claude_code").source == "https://example.com/llms.txt"
    assert not (wiki_dir / "collections").exists()


def test_migration_aborts_on_duplicate(library: Library, wikis_root: Path) -> None:
    a = wikis_root / "wiki_a"
    b = wikis_root / "wiki_b"
    a.mkdir()
    b.mkdir()
    _write_yaml(a / "collections" / "claude_code.yaml")
    _write_yaml(b / "collections" / "claude_code.yaml")
    with pytest.raises(MigrationConflictError):
        apply_migration(plan_migration(wikis_root))


def test_migration_idempotent(library: Library, wikis_root: Path) -> None:
    wiki_dir = wikis_root / "default"
    wiki_dir.mkdir()
    _write_yaml(wiki_dir / "collections" / "claude_code.yaml")
    plan_migration(wikis_root)
    apply_migration(plan_migration(wikis_root))
    # Second run after first moved: nothing to do, no error.
    plan_migration(wikis_root)
    apply_migration(plan_migration(wikis_root))


def test_cli_duplicate_surfaces_paths(
    library: Library, wikis_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator-actionable output: each duplicate-slug conflict prints
    one line listing all colliding source paths BEFORE the command exits 2.
    """
    from lies import xdg

    monkeypatch.setattr(xdg, "config_home", lambda: wikis_root.parent)

    a = wikis_root / "wiki_a"
    b = wikis_root / "wiki_b"
    a.mkdir()
    b.mkdir()
    _write_yaml(a / "collections" / "llms.yaml", name="llms")
    _write_yaml(b / "collections" / "llms.yaml", name="llms")

    runner = CliRunner()
    result = runner.invoke(app, ["migrate-collection-configs"])

    assert result.exit_code == 2, result.output
    assert "duplicate: llms" in result.output
    assert str(a / "collections" / "llms.yaml") in result.output
    assert str(b / "collections" / "llms.yaml") in result.output


def test_plan_surfaces_library_collision(library: Library, wikis_root: Path) -> None:
    """A pre-existing library config under the same slug is a pre-flight
    collision; partial-state re-run surfaces the same conflict before any
    write instead of raising ``CollectionAlreadyExists`` mid-Phase-2.
    """
    from lies.library.config_io import save_config
    from lies.library.record import LibraryCollectionConfig

    wiki_dir = wikis_root / "default"
    wiki_dir.mkdir()
    _write_yaml(wiki_dir / "collections" / "claude_code.yaml")
    # Seed the library with the same slug — simulates a prior partial run.
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

    plan = plan_migration(wikis_root)
    assert plan.duplicates == ()
    assert len(plan.library_collisions) == 1
    slug, target = plan.library_collisions[0]
    assert slug == "claude_code"
    assert target.exists()


def test_apply_aborts_on_library_collision_without_force(
    library: Library, wikis_root: Path
) -> None:
    wiki_dir = wikis_root / "default"
    wiki_dir.mkdir()
    _write_yaml(wiki_dir / "collections" / "claude_code.yaml")
    # Pre-populate the library with the same slug.
    from lies.library.config_io import save_config
    from lies.library.record import LibraryCollectionConfig

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

    with pytest.raises(MigrationConflictError):
        apply_migration(plan_migration(wikis_root))
    # Source YAML untouched because apply aborted pre-write.
    assert (wiki_dir / "collections" / "claude_code.yaml").exists()


def test_apply_force_overwrites_library_collision(library: Library, wikis_root: Path) -> None:
    """`--force` overwrites a colliding library config; the wiki YAML
    is consumed and the library record now reflects the wiki source.
    """
    wiki_dir = wikis_root / "default"
    wiki_dir.mkdir()
    _write_yaml(wiki_dir / "collections" / "claude_code.yaml")
    from lies.library.config_io import save_config
    from lies.library.record import LibraryCollectionConfig

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

    apply_migration(plan_migration(wikis_root), force=True)
    assert load_config("claude_code").source == "https://example.com/llms.txt"
    assert not (wiki_dir / "collections" / "claude_code.yaml").exists()


def test_apply_skips_vanished_source(
    library: Library, wikis_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A wiki YAML deleted between plan and apply is logged and skipped."""
    wiki_dir = wikis_root / "default"
    wiki_dir.mkdir()
    src = wiki_dir / "collections" / "claude_code.yaml"
    _write_yaml(src)
    plan = plan_migration(wikis_root)
    src.unlink()  # source vanished between plan and apply
    with caplog.at_level("INFO", logger="lies.library.migrate_collection_configs"):
        apply_migration(plan)
    # No error raised; library untouched; no crash.
    assert not src.exists()
