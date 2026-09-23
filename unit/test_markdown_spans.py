"""Tests for src/lies/markdown_spans.py — F37."""

from __future__ import annotations

import pytest

from lies.markdown_spans import Span, parse_spans


def test_parse_spans_empty_input() -> None:
    assert parse_spans("") == []


def test_parse_spans_top_level_body() -> None:
    text = "Just prose, no headings.\nSecond line.\n"
    spans = parse_spans(text)
    assert len(spans) == 1
    assert spans[0].heading_path == []
    assert spans[0].body.strip() == "Just prose, no headings.\nSecond line."
    assert spans[0].code_fence is False
    assert spans[0].start_line == 1


def test_parse_spans_single_h1() -> None:
    text = "# Title\n\nBody under h1.\n\n## Sub\n\nBody under h2.\n"
    spans = parse_spans(text)
    assert len(spans) == 2
    assert spans[0].heading_path == ["Title"]
    assert "Body under h1" in spans[0].body
    assert spans[1].heading_path == ["Title", "Sub"]
    assert "Body under h2" in spans[1].body


def test_parse_spans_nested_h2_h3() -> None:
    text = "## A\n\ntext A\n\n### A.1\n\ntext A.1\n\n## B\n\ntext B\n"
    spans = parse_spans(text)
    assert [s.heading_path for s in spans] == [
        ["A"],
        ["A", "A.1"],
        ["B"],
    ]


def test_parse_spans_code_fence_excluded_from_text_body() -> None:
    text = "# Heading\n\nProse before.\n\n```python\nx = 1\n```\n\nProse after.\n"
    spans = parse_spans(text)
    assert len(spans) >= 2
    code_spans = [s for s in spans if s.code_fence]
    assert len(code_spans) == 1
    assert code_spans[0].body == "x = 1"
    prose_spans = [s for s in spans if not s.code_fence]
    assert all("```" not in s.body for s in prose_spans)


def test_parse_spans_indented_hash_not_heading() -> None:
    text = "# Real\n\n    # Not a heading\n\nStill body.\n"
    spans = parse_spans(text)
    assert spans[0].heading_path == ["Real"]
    assert "Not a heading" in spans[0].body or "Still body" in spans[0].body


def test_parse_spans_setext_skipped_v1() -> None:
    text = "Heading\n=======\n\nBody.\n"
    spans = parse_spans(text)
    # Setext out of scope for v1 — top-level body, no heading path
    assert spans[0].heading_path == []


def test_parse_spans_start_lines_one_indexed() -> None:
    text = "\n\n# Heading at line 3\n\nBody line 5.\n"
    spans = parse_spans(text)
    assert spans[0].start_line == 3


def test_parse_spans_returns_frozen_dataclass() -> None:
    spans = parse_spans("# H\n\nbody\n")
    assert isinstance(spans[0], Span)
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        spans[0].heading_path = ["other"]  # type: ignore[misc]
