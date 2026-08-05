"""End-to-end LiquidBuilder integration through BespokeBuilder."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lies.builders.bespoke import BespokeBuilder
from lies.collections.record import Collection


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.liquid").write_bytes(b"<h1>Welcome to {{ shop.name }}</h1>")
    manifest = {
        "files": [
            {
                "path": "index.liquid",
                "source_format": "liquid",
                "out_path": "index.md",
            }
        ]
    }
    (src / "manifest.json").write_text(json.dumps(manifest))
    return src


def _collection(tmp_path: Path, *, config: dict | None = None) -> Collection:
    now = datetime.now(tz=timezone.utc)
    return Collection(
        name="liquid-fixture",
        path=tmp_path,
        source="",
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=now,
        updated_at=now,
        config=config or {},
    )


def test_bespoke_dispatches_liquid(workspace: Path, tmp_path: Path) -> None:
    """BespokeBuilder resolves source_format=liquid and dispatches to LiquidBuilder."""
    docs = BespokeBuilder().build(workspace, collection=_collection(tmp_path))
    assert len(docs) == 1
    doc = docs[0]
    assert doc.path == "index.md"
    assert doc.source_format == "markdown"
    md = doc.content.decode("utf-8")
    assert "Welcome" in md
    assert doc.source_sha256 == hashlib.sha256(doc.content).hexdigest()


def test_bespoke_liquid_with_render_cmd(workspace: Path, tmp_path: Path) -> None:
    """render_cmd stub wraps the source before pandoc sees it."""
    docs = BespokeBuilder().build(
        workspace,
        collection=_collection(
            tmp_path,
            config={
                "render_cmd": "tests.fixtures.liquid_stub:render",
                "context": {"shop": {"name": "Stub Shop"}},
            },
        ),
    )
    md = docs[0].content.decode("utf-8")
    # The stub wraps the source in <html><body>...</body></html>; the
    # pre-render Liquid tags end up in the rendered markdown body.
    assert "Welcome to {{ shop.name }}" in md
