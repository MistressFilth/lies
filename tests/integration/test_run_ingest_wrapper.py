"""Verify Orchestrator.run_ingest delegates to sync_helper.sync_collection.

Task 27 replaces the legacy agent-based ingest path with a thin
wrapper around ``sync_helper.sync_collection``. The wrapper still
returns ``"ingested {source}"`` to keep callers compiling.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from lies.orchestrator import Orchestrator
from tests.conftest import make_wiki, models_for_tests


def test_run_ingest_delegates_to_sync_collection(tmp_path: Path) -> None:
    wiki = make_wiki(name="ingest-1", data_root=tmp_path)
    o = Orchestrator(wiki=wiki, models=models_for_tests("test"))
    with mock.patch("lies.etl.sync_helper.sync_collection") as m:
        o.run_ingest("source.md", no_llm=True)
    m.assert_called_once()
    args, kwargs = m.call_args
    assert args[0] is o.wiki
    assert args[1] == "source"
    assert kwargs == {"force": False}


def test_run_ingest_preserves_existing_return_type(tmp_path: Path) -> None:
    wiki = make_wiki(name="ingest-2", data_root=tmp_path)
    o = Orchestrator(wiki=wiki, models=models_for_tests("test"))
    with mock.patch("lies.etl.sync_helper.sync_collection"):
        result = o.run_ingest("source.md", no_llm=True)
    assert result == "ingested source.md"


def test_run_ingest_derives_collection_name_from_stem(tmp_path: Path) -> None:
    """The collection name is the source's filename stem, not the full path."""
    wiki = make_wiki(name="ingest-3", data_root=tmp_path)
    o = Orchestrator(wiki=wiki, models=models_for_tests("test"))
    with mock.patch("lies.etl.sync_helper.sync_collection") as m:
        o.run_ingest("raw/articles/sample-article.md", no_llm=True)
    args, _ = m.call_args
    assert args[1] == "sample-article"
