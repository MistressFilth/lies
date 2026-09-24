from lies.query.formats.md import render_markdown


def test_render_markdown_adds_trailing_newline() -> None:
    assert render_markdown("hello") == "hello\n"


def test_render_markdown_preserves_trailing_newline() -> None:
    assert render_markdown("hello\n") == "hello\n"


def test_render_markdown_empty_body() -> None:
    assert render_markdown("") == ""
