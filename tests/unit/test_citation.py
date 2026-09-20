from typing import get_type_hints

from lies.query.citation import Citation, ClaimCitation


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


def test_claim_citation_construction() -> None:
    cc = ClaimCitation(claim="Each subagent runs in its own context.", citation_index=0)
    assert cc.claim == "Each subagent runs in its own context."
    assert cc.citation_index == 0


def test_claim_citation_equality() -> None:
    a = ClaimCitation(claim="x", citation_index=1)
    b = ClaimCitation(claim="x", citation_index=1)
    assert a == b


def test_claim_citation_frozen() -> None:
    cc = ClaimCitation(claim="x", citation_index=0)
    try:
        cc.citation_index = 99  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ClaimCitation should be frozen")


def test_claim_citation_annotations() -> None:
    from typing import get_type_hints

    hints = get_type_hints(ClaimCitation)
    assert hints["claim"] is str
    assert hints["citation_index"] is int


# ---------------------------------------------------------------------------
# F19: heading_path on Citation, quote on ClaimCitation
# ---------------------------------------------------------------------------


def test_citation_has_heading_path_default_none() -> None:
    c = Citation(path="wiki/x.md", source="wiki")
    assert c.heading_path is None


def test_citation_heading_path_settable() -> None:
    c = Citation(path="wiki/x.md", source="wiki", heading_path=["H1", "H2"])
    assert c.heading_path == ["H1", "H2"]


def test_citation_frozen() -> None:
    c = Citation(path="wiki/x.md", source="wiki")
    import pytest

    with pytest.raises(Exception):
        c.heading_path = ["H1"]  # type: ignore[misc]


def test_claim_citation_has_quote_default_empty() -> None:
    cc = ClaimCitation(claim="x", citation_index=0)
    assert cc.quote == ""


def test_claim_citation_quote_settable() -> None:
    cc = ClaimCitation(claim="x", citation_index=0, quote='verbatim "quoted"')
    assert cc.quote == 'verbatim "quoted"'
