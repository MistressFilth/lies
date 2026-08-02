from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lies.builders.base import (
    BuilderRegistry,
    PassThroughBuilder,
)
from lies.builders.errors import BuilderUnavailable
from lies.collections.record import Collection


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "source.md").write_text("# hello\n", encoding="utf-8")
    return tmp_path


def test_registry_resolve_raises_when_unregistered() -> None:
    reg = BuilderRegistry()
    with pytest.raises(BuilderUnavailable) as exc:
        reg.resolve("liquid")
    assert exc.value.source_format == "liquid"


def test_registry_round_trip() -> None:
    reg = BuilderRegistry()
    builder = PassThroughBuilder()
    reg.register("markdown", builder)
    assert reg.resolve("markdown") is builder
    assert reg.formats() == {"markdown"}


def test_pass_through_returns_single_doc(workspace: Path) -> None:
    now = datetime.now(tz=timezone.utc)
    c = Collection(
        name="x",
        path=workspace,
        source="",
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=now,
        updated_at=now,
    )
    docs = PassThroughBuilder().build(workspace, collection=c)
    assert len(docs) == 1
    assert docs[0].source_format == "markdown"
    assert docs[0].path == "source.md"
    assert b"hello" in docs[0].content
