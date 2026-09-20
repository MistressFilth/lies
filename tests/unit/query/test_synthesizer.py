"""Tests for PageRead and synthesizer builders — F19."""

from __future__ import annotations

import dataclasses

from lies.markdown_spans import Span
from lies.query.synthesizer import PageRead


def test_page_read_has_spans_field() -> None:
    pr = PageRead(
        rel_path="wiki/x.md",
        title="X",
        spans=[Span(heading_path=["H1"], body="body", code_fence=False, start_line=1)],
        source="wiki",
    )
    assert len(pr.spans) == 1
    assert pr.spans[0].heading_path == ["H1"]


def test_page_read_excerpt_field_removed() -> None:
    fields = {f.name for f in dataclasses.fields(PageRead)}
    assert "excerpt" not in fields
    assert "spans" in fields


def test_page_read_source_required_no_default() -> None:
    fields = {f.name: f for f in dataclasses.fields(PageRead)}
    assert fields["source"].default is dataclasses.MISSING
