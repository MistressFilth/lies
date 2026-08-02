import pytest

from lies.etl.normalize.format_dispatch import UnknownFormatError, dispatch
from lies.etl.normalize.obsidian import apply


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
