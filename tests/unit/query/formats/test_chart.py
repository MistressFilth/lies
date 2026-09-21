"""Renderer tests for the chart (Mermaid) output format (F1 addendum)."""

from __future__ import annotations

from lies.query.formats.chart import render_chart


def test_render_chart_zero_blocks_returns_body_unchanged() -> None:
    body = "no diagram here, just prose.\n"
    assert render_chart(body) == body


def test_render_chart_single_block_returns_block() -> None:
    body = "```mermaid\ngraph LR\n  A --> B\n```\n"
    assert render_chart(body) == "graph LR\n  A --> B"


def test_render_chart_multiple_blocks_returns_longest() -> None:
    body = (
        "```mermaid\ngraph LR\n  A --> B\n```\n"
        "```mermaid\nsequenceDiagram\n  participant A\n  participant B\n  A->>B: hi\n  B->>A: bye\n```\n"
    )
    assert render_chart(body) == (
        "sequenceDiagram\n  participant A\n  participant B\n  A->>B: hi\n  B->>A: bye"
    )


def test_render_chart_preserves_optional_caption_around_fence() -> None:
    body = "Caption above.\n\n```mermaid\ngraph TD\n  X --> Y\n```\n\nTrailer.\n"
    # Only the fence body is returned; caption + trailer dropped.
    assert render_chart(body) == "graph TD\n  X --> Y"


def test_render_chart_handles_class_diagram() -> None:
    body = "```mermaid\nclassDiagram\n  class Animal\n  Animal : +name string\n```\n"
    assert render_chart(body) == "classDiagram\n  class Animal\n  Animal : +name string"


def test_render_chart_empty_body() -> None:
    assert render_chart("") == ""


def test_render_chart_ignores_mermaid_keyword_outside_fence() -> None:
    body = "The word mermaid appears here but no fence.\n"
    assert render_chart(body) == body


def test_render_chart_ignores_fenced_block_without_mermaid_lang() -> None:
    body = "```python\nprint('hi')\n```\n"
    assert render_chart(body) == body


def test_render_chart_accepts_trailing_space_after_lang_tag() -> None:
    """Markdown spec permits `` ```mermaid `` (trailing space after lang).

    The renderer must accept this form, not just the strict
    `` ```mermaid\\n `` newline form.
    """
    body = "```mermaid \ngraph LR\n  A --> B\n```\n"
    assert render_chart(body) == "graph LR\n  A --> B"


def test_render_chart_accepts_tab_after_lang_tag() -> None:
    """Tabs count as whitespace after the lang tag (per Markdown spec)."""
    body = "```mermaid\t\ngraph TD\n  X --> Y\n```\n"
    assert render_chart(body) == "graph TD\n  X --> Y"
