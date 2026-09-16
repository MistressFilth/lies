from lies.orchestrator import _render_footnote_line, _render_footnotes
from lies.query.citation import Citation


def test_render_footnote_line_with_line_and_section() -> None:
    c = Citation(
        path="claude_code/agent-sdk/subagents.md",
        source="wiki",
        line=42,
        section="Context isolation",
    )
    out = _render_footnote_line(1, c, title="Subagents in the SDK")
    assert out == (
        "[^1]: [Subagents in the SDK](claude_code/agent-sdk/subagents.md#L42) — Context isolation"
    )


def test_render_footnote_line_with_section_only() -> None:
    c = Citation(
        path="claude_code/foo.md",
        source="library",
        section="S",
    )
    out = _render_footnote_line(2, c, title="Foo")
    assert out == "[^2]: [Foo](claude_code/foo.md#s) — S"


def test_render_footnote_line_with_line_only() -> None:
    c = Citation(path="x.md", source="wiki", line=10)
    out = _render_footnote_line(3, c, title="X")
    assert out == "[^3]: [X](x.md#L10)"


def test_render_footnote_line_with_neither() -> None:
    c = Citation(path="x.md", source="wiki")
    out = _render_footnote_line(4, c, title="X")
    assert out == "[^4]: [X](x.md)"


def test_render_footnotes_empty() -> None:
    assert _render_footnotes([], page_titles={}) == ""


def test_render_footnotes_full_block() -> None:
    citations = [
        Citation(path="a.md", source="wiki", line=10, section="Top"),
        Citation(path="b.md", source="library", line=20, section="Mid"),
    ]
    block = _render_footnotes(
        citations,
        page_titles={"a.md": "A Page", "b.md": "B Page"},
    )
    expected = "Footnotes:\n\n[^1]: [A Page](a.md#L10) — Top\n[^2]: [B Page](b.md#L20) — Mid"
    assert block == expected


def test_render_footnotes_missing_title_falls_back_to_path() -> None:
    citations = [Citation(path="a.md", source="wiki", line=10)]
    block = _render_footnotes(citations, page_titles={})
    assert "[^1]: [a.md](a.md#L10)" in block
