"""MCP ingest_source requires --collection and runs the bootstrap path."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import pytest

from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki


@pytest.fixture
def wiki(tmp_path: Path) -> Wiki:
    root = tmp_path / "wiki"
    root.mkdir()
    return make_wiki(name="mcp-bootstrap", data_root=root)


def test_ingest_source_tool_signature_requires_collection() -> None:
    from lies.mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert "ingest_source" in by_name
    params = by_name["ingest_source"].parameters
    assert "collection" in params["properties"]
    assert "collection" in params.get("required", [])


def test_ingest_source_calls_bootstrap_then_sync_collection(
    wiki: Wiki, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lies.mcp.server import ingest_source

    monkeypatch.setenv("LIES_WIKI_NAME", wiki.name)
    # ``bootstrap_collection`` runs for real (writes the YAML) so the
    # post-condition ``(wiki.collections_dir / "alpha.yaml").exists()``
    # is meaningful. We only mock ``ensure_wiki`` (skip real XDG init)
    # and ``sync_collection`` (skip the heavy ETL stack).
    with (
        mock.patch("lies.etl.sync_helper.sync_collection") as mock_sync,
        mock.patch("lies.collections.bootstrap.ensure_wiki", return_value=wiki),
    ):
        result = ingest_source(
            source="https://example.com/llms.txt",
            collection="alpha",
            name=wiki.name,
            no_llm=True,
        )
    assert result == "ingested https://example.com/llms.txt into alpha (no_llm)"
    assert (wiki.collections_dir / "alpha.yaml").exists()
    # MCP tool passes the explicit ``collection`` through to
    # ``sync_collection``; the URL stem (``llms``) is ignored.
    mock_sync.assert_called_once()
    args, kwargs = mock_sync.call_args
    assert args[0] == wiki
    assert args[1] == "alpha"
    assert kwargs == {"force": False}


def test_ingest_source_collision_raises_value_error(
    wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lies.collections.errors import CollectionMismatch
    from lies.mcp.server import ingest_source

    monkeypatch.setenv("LIES_WIKI_NAME", wiki.name)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "alpha.yaml").write_text(
        "name: alpha\npath: /raw/alpha\nsource: https://OLD.example.com\n"
        "tags: []\nscraper_cmd: null\ndoc_path: null\nmapper_model: null\n"
        "language: null\nversion: '1'\n"
        "created_at: 2026-01-01T00:00:00+00:00\nupdated_at: 2026-01-01T00:00:00+00:00\n"
        "config: {}\n",
        encoding="utf-8",
    )
    with (
        mock.patch("lies.collections.bootstrap.ensure_wiki", return_value=wiki),
        mock.patch(
            "lies.collections.bootstrap.bootstrap_collection",
            side_effect=CollectionMismatch(
                existing_source="https://OLD.example.com",
                existing_format=None,
                requested_source="https://new.example.com/llms.txt",
                requested_format=None,
            ),
        ),
        pytest.raises(ValueError, match="OLD.example.com"),
    ):
        ingest_source(
            source="https://new.example.com/llms.txt",
            collection="alpha",
            name=wiki.name,
        )
