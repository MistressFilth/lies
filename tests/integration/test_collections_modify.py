"""Integration tests for `lies collections modify` end-to-end behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.collections.record import (
    Collection,
    load_collection,
    save_collection,
)
from lies.wiki.wiki import Wiki

runner = CliRunner()


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    name = "modify"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LIES_XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LIES_XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LIES_XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    wiki = Wiki(
        name=name,
        data_root=xdg.data_home() / "lies" / name,
        config_root=xdg.config_home() / "lies" / name,
        cache_root=xdg.cache_home() / "lies" / name,
        state_root=xdg.state_home() / "lies" / name,
        runtime_root=xdg.runtime_dir_for(name),
    )
    wiki.data_root.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    return wiki


def _seed(wiki: Wiki, name: str) -> None:
    save_collection(
        wiki,
        Collection(
            name=name,
            path=PurePosixPath(f"/raw/{name}"),
            source="https://old.example.com",
            tags=["old"],
            scraper_cmd=None,
            doc_path=None,
            mapper_model=None,
            language=None,
            version="1",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            config={},
        ),
    )


def test_modify_round_trip(wiki: Wiki) -> None:
    _seed(wiki, "cpython")
    result = runner.invoke(
        app,
        [
            "collections",
            "modify",
            "cpython",
            "--set",
            "tags=stdlib,core",
            "--set",
            "language=en",
        ],
    )
    assert result.exit_code == 0, result.output

    show = runner.invoke(app, ["collections", "show", "cpython"])
    assert "tags=['stdlib', 'core']" in show.output
    assert "language=en" not in show.output or "source=" in show.output

    loaded = load_collection(wiki, "cpython")
    assert loaded.tags == ["stdlib", "core"]
    assert loaded.language == "en"
    assert loaded.source == "https://old.example.com"  # preserved
    assert loaded.created_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert loaded.updated_at > datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_modify_atomic_write_no_partial_file(wiki: Wiki) -> None:
    _seed(wiki, "cpython")
    target = Collection.config_path(wiki, "cpython")
    before = target.read_text(encoding="utf-8")

    # Simulate crash: tmp writes fine, os.replace raises
    with mock.patch("os.replace", side_effect=OSError("simulated crash")):
        result = runner.invoke(
            app,
            ["collections", "modify", "cpython", "--set", "tags=broken"],
        )
    assert result.exit_code != 0

    # Original file untouched
    after = target.read_text(encoding="utf-8")
    assert before == after

    # No tmp file left behind
    sibling_tmp = target.with_suffix(target.suffix + ".tmp")
    assert not sibling_tmp.exists()

    # Reload still works
    loaded = load_collection(wiki, "cpython")
    assert loaded.tags == ["old"]
