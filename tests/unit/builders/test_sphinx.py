from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from lies.builders.errors import BuilderFetchFailed
from lies.builders.sphinx import SphinxBuilder
from lies.collections.record import Collection


@pytest.fixture
def collection_factory(tmp_path: Path):
    def _make(config: dict) -> Collection:
        return Collection(
            name="sphinx_test",
            path=tmp_path / "raw" / "sphinx_test",
            source="",
            tags=[],
            scraper_cmd=None,
            doc_path=None,
            mapper_model=None,
            language=None,
            version="1.0.0",
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
            config=config,
        )

    return _make


@pytest.fixture
def seeded_workspace(tmp_path: Path) -> Path:
    """A workspace at ``<tmp_path>/ws`` with ``src/`` populated."""
    ws = tmp_path / "ws"
    src = ws / "src"
    src.mkdir(parents=True)
    files = {
        "index.rst": "Title\n=====\n",
        "guide/intro.rst": "Intro\n=====\n",
        "_templates/sidebar.rst": "Sidebar\n========\n",
        "examples/sample.rst": "Sample\n======\n",
    }
    for rel, body in files.items():
        target = src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return ws


def test_sphinx_builder_walks_includes_only(collection_factory, seeded_workspace: Path) -> None:
    collection = collection_factory(
        {
            "sphinx_includes": ["**/*.rst"],
            "sphinx_excludes": ["_templates/**", "examples/**"],
        }
    )
    with mock.patch(
        "lies.etl.normalize.pandoc_daemon.PandocDaemon.convert",
        side_effect=lambda raw, fmt: f"# from {fmt}\n".encode(),
    ):
        docs = SphinxBuilder().build(seeded_workspace, collection=collection)
    paths = sorted(d.path for d in docs)
    assert paths == ["guide/intro.md", "index.md"]
    assert all(d.source_format == "markdown" for d in docs)


def test_sphinx_builder_applies_renames(collection_factory, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    src = ws / "src"
    src.mkdir(parents=True)
    (src / "old.rst").write_text("Old\n===\n", encoding="utf-8")
    collection = collection_factory({"sphinx_renames": {"old.rst": "renamed/new.md"}})
    with mock.patch(
        "lies.etl.normalize.pandoc_daemon.PandocDaemon.convert",
        side_effect=lambda raw, fmt: b"x",
    ):
        docs = SphinxBuilder().build(ws, collection=collection)
    assert [d.path for d in docs] == ["renamed/new.md"]


def test_sphinx_builder_raises_on_pandoc_failure(collection_factory, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    src = ws / "src"
    src.mkdir(parents=True)
    (src / "index.rst").write_text("x", encoding="utf-8")
    collection = collection_factory({})
    with (
        mock.patch(
            "lies.etl.normalize.pandoc_daemon.PandocDaemon.convert",
            side_effect=RuntimeError("exit 47"),
        ),
        pytest.raises(BuilderFetchFailed),
    ):
        SphinxBuilder().build(ws, collection=collection)


def test_sphinx_builder_is_registered() -> None:
    from lies.builders.base import REGISTRY
    from lies.builders.sphinx import SphinxBuilder

    assert "sphinx" in REGISTRY.formats()
    assert isinstance(REGISTRY.resolve("sphinx"), SphinxBuilder)
