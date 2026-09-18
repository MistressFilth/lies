"""Integration tests for `lies library modify` end-to-end behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.library.config_io import config_path_for, load_config, save_config
from lies.library.record import LibraryCollectionConfig

runner = CliRunner()


@pytest.fixture
def library_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin XDG to ``tmp_path`` and clear the Library cache.

    The library CLI's :func:`config_path_for` reads through
    :meth:`lies.library.paths.Library.open`, an ``lru_cache(maxsize=1)``
    singleton. Clearing the cache and pointing XDG at ``tmp_path`` lets
    the test invoke the real CLI without leaking state between runs.
    """
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    from lies.library.paths import Library

    Library.open.cache_clear()
    return tmp_path


def _seed(name: str) -> LibraryCollectionConfig:
    rec = LibraryCollectionConfig(
        name=name,
        source="https://old.example.com",
        tags=("old",),
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        config={},
    )
    save_config(rec)
    return rec


def test_modify_round_trip(library_root: Path) -> None:
    _seed("cpython")
    result = runner.invoke(
        app,
        [
            "library",
            "modify",
            "cpython",
            "--set",
            "tags=stdlib,core",
            "--set",
            "language=en",
        ],
    )
    assert result.exit_code == 0, result.output

    show = runner.invoke(app, ["library", "show", "cpython"])
    assert "tags=" in show.output
    assert "stdlib" in show.output
    assert "core" in show.output

    loaded = load_config("cpython")
    assert loaded.tags == ("stdlib", "core")
    assert loaded.language == "en"
    assert loaded.source == "https://old.example.com"  # preserved
    assert loaded.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert loaded.updated_at > datetime(2026, 1, 1, tzinfo=UTC)


def test_modify_atomic_write_no_partial_file(library_root: Path) -> None:
    _seed("cpython")
    target = config_path_for("cpython")
    before = target.read_text(encoding="utf-8")

    # Simulate crash: tmp writes fine, os.replace raises
    with mock.patch("os.replace", side_effect=OSError("simulated crash")):
        result = runner.invoke(
            app,
            ["library", "modify", "cpython", "--set", "tags=broken"],
        )
    assert result.exit_code != 0

    # Original file untouched
    after = target.read_text(encoding="utf-8")
    assert before == after

    # No tmp file left behind
    sibling_tmp = target.with_suffix(target.suffix + ".tmp")
    assert not sibling_tmp.exists()

    # Reload still works
    loaded = load_config("cpython")
    assert loaded.tags == ("old",)
