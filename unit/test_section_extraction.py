"""Tests for _extract_section_at."""

from lies.query.section import _extract_section_at


def test_heading_at_line() -> None:
    body = "# Top\n\n## Sub\n\ntext\n"
    assert _extract_section_at(body, 4) == "Sub"


def test_heading_above_line() -> None:
    body = "# Top\n\n## Sub\n\ntext on line 5\n"
    assert _extract_section_at(body, 5) == "Sub"


def test_no_heading_anywhere() -> None:
    body = "line one\nline two\nline three\n"
    assert _extract_section_at(body, 2) is None


def test_line_past_eof_returns_last_heading() -> None:
    body = "# Top\n\n## Sub\n"
    assert _extract_section_at(body, 100) == "Sub"


def test_indented_hash_not_a_heading() -> None:
    body = "# Top\n\n    # not a heading\n\ntext\n"
    # 4-space indent → not a heading per CommonMark → last heading stays "Top".
    assert _extract_section_at(body, 5) == "Top"


def test_setext_underline_not_extracted() -> None:
    """Setext headings (=== / ---) are out of scope for v1."""
    body = "Top\n===\n\nSub\n---\n\ntext\n"
    # Only ATX headings register; Setext does not.
    assert _extract_section_at(body, 6) is None


def test_empty_body() -> None:
    assert _extract_section_at("", 1) is None


def test_line_zero_or_negative() -> None:
    body = "# Top\n"
    # line <= 0 → no heading encountered → None.
    assert _extract_section_at(body, 0) is None
    assert _extract_section_at(body, -1) is None


def test_heading_with_inline_formatting() -> None:
    body = "# Top with **bold**\n\ntext\n"
    assert _extract_section_at(body, 3) == "Top with **bold**"
