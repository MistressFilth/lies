"""Tests for `lies collections show` registration status reporting."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.collections.record import Collection, save_collection

runner = CliRunner()


@pytest.fixture
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / ".lies").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LIES_WIKI_ROOT", str(tmp_path / "wiki"))
    return tmp_path / "wiki"


def test_collections_show_reports_pending(wiki: Path) -> None:
    save_collection(
        wiki,
        Collection(
            name="htmx",
            path=wiki / "raw" / "htmx",
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


def test_collections_show_reports_registered(wiki: Path) -> None:
    save_collection(
        wiki,
        Collection(
            name="htmx",
            path=wiki / "raw" / "htmx",
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
        root=PurePosixPath(str(wiki / "raw" / "htmx")),
        qmd_collection="htmx",
        schema_path=PurePosixPath(str(wiki / ".lies" / "schema.md")),
    )
    with mock.patch(
        "lies.memory.service.WikiMemoryService.registered_collections",
        return_value=[ref],
    ):
        result = runner.invoke(app, ["collections", "show", "htmx"])
    assert result.exit_code == 0
    assert "status: registered" in result.stdout
