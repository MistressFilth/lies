from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pymupdf
import pytest

from lies.builders.base import BuilderRegistry  # noqa: F401
from lies.builders.errors import BuilderParseError
from lies.builders.pdf import PDFBuilder
from lies.collections.record import Collection


def _make_pdf(tmp_path: Path, pages: list[str]) -> Path:
    out = tmp_path / "source.pdf"
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(str(out))
    doc.close()
    return out


def _collection(tmp_path: Path) -> Collection:
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
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        config={},
    )


def test_pdf_builder_emits_one_doc_per_page(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path, ["page one body", "page two body", "page three body"])  # noqa: F841
    docs = PDFBuilder().build(tmp_path, collection=_collection(tmp_path))
    assert len(docs) == 3
    assert [d.path for d in docs] == [
        "pages/page-0001.md",
        "pages/page-0002.md",
        "pages/page-0003.md",
    ]
    assert all(d.source_format == "markdown" for d in docs)
    joined = b"\n".join(d.content for d in docs)
    assert b"page one body" in joined
    assert b"page three body" in joined


def test_pdf_builder_raises_on_missing_source(tmp_path: Path) -> None:
    with pytest.raises(BuilderParseError):
        PDFBuilder().build(tmp_path, collection=_collection(tmp_path))


def test_pdf_builder_is_registered_in_default_registry() -> None:
    from lies.builders.base import REGISTRY

    assert "pdf" in REGISTRY.formats()
    assert isinstance(REGISTRY.resolve("pdf"), PDFBuilder)
