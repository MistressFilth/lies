from lies.query.formats.table import render_table


def test_render_table_identity() -> None:
    body = "| col1 | col2 |\n| --- | --- |\n| a | b |\n"
    assert render_table(body) == body


def test_render_table_preserves_trailing_newline() -> None:
    body = "| col1 | col2 |\n| --- | --- |\n| a | b |\n"
    assert render_table(body) == body
    assert body.endswith("\n")


def test_render_table_empty_body() -> None:
    assert render_table("") == ""
