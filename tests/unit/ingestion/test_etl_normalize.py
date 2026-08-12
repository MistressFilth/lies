from __future__ import annotations

from datetime import UTC, datetime
from unittest import mock

import pytest

from lies.collections.record import Collection
from lies.etl.normalize.format_dispatch import UnknownFormatError, dispatch
from lies.etl.normalize.obsidian import apply
from lies.etl.stages.normalize import run_normalize
from lies.scrapers.base import ParsedDoc
from lies.wiki.wiki import Wiki


def _wiki(tmp_path) -> Wiki:
    wiki = Wiki(
        name="x",
        data_root=tmp_path,
        config_root=tmp_path,
        cache_root=tmp_path,
        state_root=tmp_path,
        runtime_root=tmp_path,
    )
    wiki.raw_dir.mkdir(parents=True, exist_ok=True)
    wiki.scratch_dir.mkdir(parents=True, exist_ok=True)
    return wiki


def _collection(tmp_path) -> Collection:
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


def test_normalize_dispatches_sphinx_via_builder(tmp_path) -> None:
    """Sphinx-format docs route through SphinxBuilder.build via the registry."""
    c = _collection(tmp_path)
    docs = [
        ParsedDoc(
            path="index.rst",
            content=b"unused",
            source_sha256="h",
            source_format="sphinx",
        )
    ]
    with mock.patch(
        "lies.builders.sphinx.SphinxBuilder.build",
        return_value=[
            ParsedDoc(
                path="index.md",
                content=b"# Title\n",
                source_sha256="h2",
                source_format="markdown",
            )
        ],
    ):
        result = run_normalize(_wiki(tmp_path), c, docs)
    assert result.success == ["index.md"]
    assert result.quarantined == []


def test_normalize_quarantines_builder_unavailable(tmp_path) -> None:
    """Liquid format has no builder; falls through to format_dispatch and quarantines."""
    c = _collection(tmp_path)
    docs = [
        ParsedDoc(
            path="x.liquid",
            content=b"{% if x %}",
            source_sha256="h",
            source_format="liquid",
        )
    ]
    result = run_normalize(_wiki(tmp_path), c, docs)
    assert any("liquid" in reason for _, reason in result.quarantined)


def test_normalize_routes_bespoke_through_builder(tmp_path) -> None:
    """Bespoke docs route through ``BespokeBuilder.build`` like every other
    registered builder; the returned docs are post-processed with Obsidian
    frontmatter and emitted as markdown."""
    from lies.builders.bespoke import BespokeBuilder

    c = Collection(
        name="mycoll",
        path=tmp_path,
        source="",
        tags=["topic"],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        config={},
    )
    docs = [
        ParsedDoc(
            path="custom.md",
            content=b"# already markdown\n",
            source_sha256="h",
            source_format="bespoke",
        )
    ]
    fake_returned = [
        ParsedDoc(
            path="custom.md",
            content=b"# already markdown\n",
            source_sha256="h",
            source_format="markdown",
        )
    ]
    with mock.patch.object(BespokeBuilder, "build", return_value=fake_returned) as mock_build:
        result = run_normalize(_wiki(tmp_path), c, docs)
    assert mock_build.called
    assert result.quarantined == []
    assert result.success == ["custom.md"]
    assert len(result.parsed_docs) == 1
    out = result.parsed_docs[0]
    # After routing through the builder, the emitted doc is markdown.
    assert out.source_format == "markdown"
    assert out.path == "custom.md"
    decoded = out.content.decode("utf-8")
    assert "mycoll" in decoded
    assert "collection: mycoll" in decoded
    assert "# already markdown" in decoded


def test_dispatch_markdown_passthrough() -> None:
    md = "# Hello\n\nSome text."
    assert dispatch(md.encode("utf-8"), "markdown") == md


def test_dispatch_html_calls_pandoc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lies.etl.normalize.format_dispatch._pandoc_convert",
        lambda b, fmt: b"# from html\n",
    )
    out = dispatch(b"<h1>x</h1>", "html")
    assert out == "# from html\n"


def test_dispatch_pdf_calls_pdf_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lies.etl.normalize.format_dispatch._pdf_extract",
        lambda b: "page text",
    )
    assert dispatch(b"%PDF", "pdf") == "page text"


def test_dispatch_unknown_raises() -> None:
    with pytest.raises(UnknownFormatError):
        dispatch(b"x", "weirdformat")


def test_obsidian_apply_injects_frontmatter() -> None:
    md = "# Body"
    out = apply(md, frontmatter={"title": "X", "tags": ["python"]})
    assert "title: X" in out
    assert "tags:" in out
    assert "# Body" in out
