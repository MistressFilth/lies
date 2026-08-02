from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from lies.builders.base import BuilderRegistry, PassThroughBuilder
from lies.builders.bespoke import BespokeBuilder
from lies.collections.record import Collection


@pytest.fixture
def collection(tmp_path: Path) -> Collection:
    return Collection(
        name="bespoke",
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


def test_bespoke_routes_to_registered_builder(tmp_path: Path, collection: Collection) -> None:
    # Two files: one rst (will be converted by a fake), one markdown (passthrough).
    (tmp_path / "page.rst").write_text("a", encoding="utf-8")
    (tmp_path / "raw.md").write_text("z", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": "page.rst",
                        "out_path": "page.md",
                        "source_format": "rst",
                        "sha256": "h1",
                    },
                    {
                        "path": "raw.md",
                        "out_path": "raw.md",
                        "source_format": "markdown",
                        "sha256": "h2",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    reg = BuilderRegistry()
    fake_rst = mock.Mock()
    fake_rst.build.return_value = []  # irrelevant for this test
    reg.register("rst", fake_rst)
    reg.register("markdown", PassThroughBuilder())
    builder = BespokeBuilder(registry=reg)
    docs = builder.build(tmp_path, collection=collection)
    paths = sorted(d.path for d in docs)
    assert "raw.md" in paths
    fake_rst.build.assert_called_once()


def test_bespoke_passes_through_unknown_format(tmp_path: Path, collection: Collection) -> None:
    (tmp_path / "page.liquid").write_text("a", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": "page.liquid",
                        "out_path": "page.liquid",
                        "source_format": "liquid",
                        "sha256": "h1",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    reg = BuilderRegistry()
    docs = BespokeBuilder(registry=reg).build(tmp_path, collection=collection)
    assert len(docs) == 1
    assert docs[0].source_format == "liquid"
