"""Tests for `lies collections show` registration status reporting."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest
from typer.testing import CliRunner

from lies import xdg
from lies.cli import app
from lies.collections.record import Collection, save_collection
from lies.wiki.wiki import Wiki

runner = CliRunner()


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    name = "show"
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


def test_collections_show_reports_pending(wiki: Wiki) -> None:
    save_collection(
        wiki,
        Collection(
            name="htmx",
            path=wiki.data_root / "raw" / "htmx",
            source="",
            tags=[],
            scraper_cmd=None,
            doc_path=None,
            mapper_model=None,
            language=None,
            version="1.0.0",
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
            config={},
        ),
    )
    with mock.patch(
        "lies.memory.service.WikiMemoryService.registered_collections",
        return_value=[],
    ):
        result = runner.invoke(app, ["collections", "show", "htmx"])
    assert result.exit_code == 0
    assert "status: pending" in result.stdout


def test_collections_show_reports_registered(wiki: Wiki) -> None:
    save_collection(
        wiki,
        Collection(
            name="htmx",
            path=wiki.data_root / "raw" / "htmx",
            source="",
            tags=[],
            scraper_cmd=None,
            doc_path=None,
            mapper_model=None,
            language=None,
            version="1.0.0",
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
            config={},
        ),
    )
    from lies.memory.models import WikiCollectionRef

    ref = WikiCollectionRef(
        collection_id="htmx",
        root=PurePosixPath(str(wiki.data_root / "raw" / "htmx")),
        qmd_collection="htmx",
        schema_path=PurePosixPath(str(wiki.config_root / "schema.md")),
    )
    with mock.patch(
        "lies.memory.service.WikiMemoryService.registered_collections",
        return_value=[ref],
    ):
        result = runner.invoke(app, ["collections", "show", "htmx"])
    assert result.exit_code == 0
    assert "status: registered" in result.stdout
