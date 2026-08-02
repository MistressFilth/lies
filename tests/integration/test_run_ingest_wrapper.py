"""Verify Orchestrator.run_ingest delegates to sync_helper.sync_collection.

Task 27 replaces the legacy agent-based ingest path with a thin
wrapper around ``sync_helper.sync_collection``. The wrapper still
returns ``"ingested {source}"`` to keep callers compiling.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from lies.orchestrator import Orchestrator


def test_run_ingest_delegates_to_sync_collection(tmp_path: Path) -> None:
    o = Orchestrator(wiki_root=tmp_path, model="test")
    with mock.patch("lies.etl.sync_helper.sync_collection") as m:
        o.run_ingest("source.md")
    m.assert_called_once()
    args, kwargs = m.call_args
    assert args[0] == tmp_path
    assert args[1] == "source"
    assert kwargs == {"force": False}


def test_run_ingest_preserves_existing_return_type(tmp_path: Path) -> None:
    o = Orchestrator(wiki_root=tmp_path, model="test")
    with mock.patch("lies.etl.sync_helper.sync_collection"):
        result = o.run_ingest("source.md")
    assert result == "ingested source.md"


def test_run_ingest_derives_collection_name_from_stem(tmp_path: Path) -> None:
    """The collection name is the source's filename stem, not the full path."""
    o = Orchestrator(wiki_root=tmp_path, model="test")
    with mock.patch("lies.etl.sync_helper.sync_collection") as m:
        o.run_ingest("raw/articles/sample-article.md")
    args, _ = m.call_args
    assert args[1] == "sample-article"