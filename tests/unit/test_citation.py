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
