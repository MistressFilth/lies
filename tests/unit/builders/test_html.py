from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from lies.builders.errors import BuilderFetchFailed
from lies.builders.html import HTMLBuilder
from lies.collections.record import Collection


@pytest.fixture
def collection(tmp_path: Path) -> Collection:
    return Collection(
        name="x",
        path=tmp_path,
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
    )


def test_html_builder_emits_single_index_doc(tmp_path: Path, collection: Collection) -> None:
    (tmp_path / "source.html").write_text("<h1>Hi</h1><p>world</p>", encoding="utf-8")
    with mock.patch(
        "lies.etl.normalize.pandoc_daemon.PandocDaemon.convert",
        return_value=b"# Hi\n\nworld\n",
    ):
        docs = HTMLBuilder().build(tmp_path, collection=collection)
    assert len(docs) == 1
    assert docs[0].path == "index.md"
    assert docs[0].source_format == "markdown"
    assert b"world" in docs[0].content


def test_html_builder_raises_on_missing_source(tmp_path: Path, collection: Collection) -> None:
    with pytest.raises(BuilderFetchFailed):
        HTMLBuilder().build(tmp_path, collection=collection)


def test_html_builder_raises_on_pandoc_failure(tmp_path: Path, collection: Collection) -> None:
    (tmp_path / "source.html").write_text("<h1>x</h1>", encoding="utf-8")
    with (
        mock.patch(
            "lies.etl.normalize.pandoc_daemon.PandocDaemon.convert",
            side_effect=RuntimeError("pandoc died"),
        ),
        pytest.raises(BuilderFetchFailed),
    ):
        HTMLBuilder().build(tmp_path, collection=collection)


def test_html_builder_is_registered_in_default_registry() -> None:
    from lies.builders.base import REGISTRY
    from lies.builders.html import HTMLBuilder

    assert "html" in REGISTRY.formats()
    assert isinstance(REGISTRY.resolve("html"), HTMLBuilder)
