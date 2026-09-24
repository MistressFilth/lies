from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.library.collections_cli import library_collections_app
from lies.library.config_io import load_config, save_config
from lies.library.paths import Library
from lies.library.record import LibraryCollectionConfig


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


def _cfg(name: str) -> LibraryCollectionConfig:
    now = datetime.now(tz=UTC)
    return LibraryCollectionConfig(
        name=name,
        source=f"https://example.com/{name}",
        tags=("a",),
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=now,
        updated_at=now,
        config={},
    )


def test_list_emits_names(library: Library) -> None:
    save_config(_cfg("alpha"))
    save_config(_cfg("beta"))
    runner = CliRunner()
    app.add_typer(library_collections_app, name="library")
    result = runner.invoke(app, ["library", "list"])
    assert result.exit_code == 0
    assert "alpha" in result.stdout
    assert "beta" in result.stdout


def test_modify_dotted_key_writes_into_config_dict(library: Library) -> None:
    """`--set config.<subkey>=value` (Liquid/Sphinx builder knobs) round-trips.

    Regression: c828a18 documented dotted keys in README but the new CLI
    rejected them. Restore the legacy dotted-key path so
    `config.render_cmd`, `config.sphinx_excludes`, etc. are editable again.
    """
    save_config(_cfg("liquid"))
    runner = CliRunner()
    app.add_typer(library_collections_app, name="library")
    result = runner.invoke(
        app,
        [
            "library",
            "modify",
            "liquid",
            "--set",
            "config.render_cmd=liquid_renderer:render",
            "--set",
            "config.preserve_case=true",
        ],
    )
    assert result.exit_code == 0, result.output
    loaded = load_config("liquid")
    assert loaded.config == {
        "render_cmd": "liquid_renderer:render",
        "preserve_case": True,
    }


def test_modify_from_file_missing_path_errors(library: Library, tmp_path) -> None:
    save_config(_cfg("alpha"))
    runner = CliRunner()
    app.add_typer(library_collections_app, name="library")
    result = runner.invoke(
        app,
        ["library", "modify", "alpha", "--from-file", str(tmp_path / "no-such.yaml")],
    )
    assert result.exit_code != 0
    assert "not found" in (result.output or "")


def test_modify_from_file_malformed_yaml_errors(library: Library, tmp_path) -> None:
    save_config(_cfg("alpha"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: alpha\nsource: [unterminated\n", encoding="utf-8")
    runner = CliRunner()
    app.add_typer(library_collections_app, name="library")
    result = runner.invoke(
        app,
        ["library", "modify", "alpha", "--from-file", str(bad)],
    )
    assert result.exit_code != 0
    assert "invalid YAML" in (result.output or "")


def test_modify_from_file_null_config_normalizes_to_empty_dict(library: Library, tmp_path) -> None:
    save_config(_cfg("alpha"))
    patch = tmp_path / "patch.yaml"
    # `config:` with no value (parsed as None) must not crash the schema.
    # Only editable fields go in the patch; name/source come from the
    # existing record (set by `_seed`).
    patch.write_text("config:\n", encoding="utf-8")
    runner = CliRunner()
    app.add_typer(library_collections_app, name="library")
    result = runner.invoke(
        app,
        ["library", "modify", "alpha", "--from-file", str(patch)],
    )
    assert result.exit_code == 0, result.output
    loaded = load_config("alpha")
    assert loaded.config == {}


def test_modify_set_source_round_trip(library: Library) -> None:
    """`--set source=URL` writes through; load_config returns the new source."""
    save_config(_cfg("alpha"))
    runner = CliRunner()
    app.add_typer(library_collections_app, name="library")
    result = runner.invoke(
        app,
        [
            "library",
            "modify",
            "alpha",
            "--set",
            "source=https://new.example.com/alpha",
        ],
    )
    assert result.exit_code == 0, result.output
    loaded = load_config("alpha")
    assert loaded.source == "https://new.example.com/alpha"
