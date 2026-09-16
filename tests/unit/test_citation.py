from typing import get_type_hints

from lies.query.citation import Citation


def test_citation_construction() -> None:
    c = Citation(path="claude_platform/foo.md", source="library")
    assert c.path == "claude_platform/foo.md"
    assert c.source == "library"


def test_citation_equality() -> None:
    a = Citation(path="x.md", source="wiki")
    b = Citation(path="x.md", source="wiki")
    assert a == b


def test_citation_distinct_sources_unequal() -> None:
    a = Citation(path="x.md", source="wiki")
    b = Citation(path="x.md", source="library")
    assert a != b


def test_citation_accepts_line_and_section() -> None:
    c = Citation(
        path="claude_platform/foo.md",
        source="library",
        line=42,
        section="Context isolation",
    )
    assert c.line == 42
    assert c.section == "Context isolation"


def test_citation_line_and_section_default_none() -> None:
    c = Citation(path="x.md", source="wiki")
    assert c.line is None
    assert c.section is None


def test_citation_equality_includes_line_and_section() -> None:
    a = Citation(path="x.md", source="wiki", line=10, section="S")
    b = Citation(path="x.md", source="wiki", line=10, section="S")
    c = Citation(path="x.md", source="wiki", line=11, section="S")
    assert a == b
    assert a != c


def test_citation_annotations_include_line_and_section() -> None:
    hints = get_type_hints(Citation)
    assert hints["line"] == int | None
    assert hints["section"] == str | None
