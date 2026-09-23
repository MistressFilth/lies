"""Tests for PageRead and synthesizer builders — F19."""

from __future__ import annotations

import dataclasses

from lies.markdown_spans import Span
from lies.query.citation import Citation
from lies.query.synthesizer import PageRead, build_answer_from_pages


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


# ---------------------------------------------------------------------------
# Task 3 — extractive fallback emits [[slug]]: "verbatim" form
# ---------------------------------------------------------------------------
# The brief pins the inline citation shape produced by
# ``build_answer_from_pages`` when the LLM synthesis path is unavailable.
# Each ``PageRead`` produces one bullet of the form::
#
#     - [[<bare-slug>]] (<heading-path-or-(top of page)>): "<excerpt>"
#
# Code-fence spans are excluded; missing headings fall back to the literal
# ``(top of page)`` marker; no ``Footnotes:`` block is appended. The
# ``_excerpt_from_spans`` placeholder from Task 2 is gone — the per-span
# pick logic lives inline in :func:`build_answer_from_pages`.


def test_build_answer_from_pages_emits_inline_citation_form() -> None:
    """Extractive fallback uses ``[[slug]]: "verbatim"`` form (F19)."""
    pages = [
        PageRead(
            rel_path="wiki/concepts/pydantic.md",
            title="Pydantic",
            spans=[
                Span(
                    heading_path=["Definition", "Nested Models"],
                    body="Nested models are validated recursively.\n",
                    code_fence=False,
                    start_line=1,
                ),
            ],
            source="wiki",
        ),
    ]
    answer = build_answer_from_pages(
        question="How does pydantic handle nested models?",
        pages=pages,
        fallback_reason="",
    )
    expected = (
        "[[concepts/pydantic]] (Definition > Nested Models): "
        '"Nested models are validated recursively."'
    )
    assert expected in answer.answer
    assert "Footnotes" not in answer.answer
    # The bullet must carry a leading ``-`` to remain a markdown list item.
    assert f"- {expected}" in answer.answer
    # Citation list still threads the page through the envelope.
    assert answer.citations == [Citation(path="wiki/concepts/pydantic.md", source="wiki")]


def test_build_answer_from_pages_skips_code_fence_spans() -> None:
    """Code-fence spans are excluded from the per-page excerpt.

    A page whose first non-empty span is a code fence falls through to
    the next prose span; if only code-fence spans exist the bullet
    shows the ``(no extractable content)`` marker so the line still
    carries the citation.
    """
    pages = [
        PageRead(
            rel_path="wiki/x.md",
            title="X",
            spans=[
                Span(heading_path=["H1"], body="prose body\n", code_fence=False, start_line=1),
                Span(heading_path=["H1"], body="x = 1\n", code_fence=True, start_line=3),
            ],
            source="wiki",
        ),
    ]
    answer = build_answer_from_pages(question="q", pages=pages, fallback_reason="")
    # The code-fence body never appears in the bullet text.
    assert "x = 1" not in answer.answer
    # The prose body wins as the per-page excerpt.
    assert '"prose body"' in answer.answer


def test_build_answer_from_pages_falls_back_to_top_of_page_when_no_heading() -> None:
    """A span with no heading context renders with the ``(top of page)`` marker."""
    pages = [
        PageRead(
            rel_path="wiki/x.md",
            title="X",
            spans=[
                Span(heading_path=[], body="only prose\n", code_fence=False, start_line=1),
            ],
            source="wiki",
        ),
    ]
    answer = build_answer_from_pages(question="q", pages=pages, fallback_reason="")
    assert "(top of page)" in answer.answer and "((top of page))" not in answer.answer


def test_build_answer_from_pages_no_prose_span_uses_marker() -> None:
    """When every span is a code fence, the bullet falls back to the
    ``(no extractable content)`` marker so the citation still surfaces.
    """
    pages = [
        PageRead(
            rel_path="wiki/code-only.md",
            title="Code Only",
            spans=[
                Span(heading_path=["H1"], body="x = 1\n", code_fence=True, start_line=1),
            ],
            source="wiki",
        ),
    ]
    answer = build_answer_from_pages(question="q", pages=pages, fallback_reason="")
    assert "(no extractable content)" in answer.answer
    # The code-fence body never leaks into the bullet.
    assert "x = 1" not in answer.answer


def test_build_answer_from_pages_caps_excerpt_at_first_paragraph() -> None:
    """The per-page excerpt is trimmed to the first paragraph.

    A span whose body contains a blank line (paragraph break) yields
    only the first paragraph in the bullet; the rest stays in the page
    on disk and is not duplicated in the answer body.
    """
    pages = [
        PageRead(
            rel_path="wiki/multi.md",
            title="Multi",
            spans=[
                Span(
                    heading_path=["H1"],
                    body="first paragraph\n\nsecond paragraph\n",
                    code_fence=False,
                    start_line=1,
                ),
            ],
            source="wiki",
        ),
    ]
    answer = build_answer_from_pages(question="q", pages=pages, fallback_reason="")
    assert '"first paragraph"' in answer.answer
    assert "second paragraph" not in answer.answer


def test_build_answer_from_pages_strips_trailing_whitespace() -> None:
    """The verbatim excerpt has trailing whitespace stripped."""
    pages = [
        PageRead(
            rel_path="wiki/ws.md",
            title="WS",
            spans=[
                Span(
                    heading_path=["H1"],
                    body="   \nbody line   \n\n",
                    code_fence=False,
                    start_line=1,
                ),
            ],
            source="wiki",
        ),
    ]
    answer = build_answer_from_pages(question="q", pages=pages, fallback_reason="")
    assert '"body line"' in answer.answer


def test_build_answer_from_pages_emits_bare_slug() -> None:
    """The ``[[slug]]`` form uses the path minus its collection prefix
    and ``.md`` suffix.

    ``wiki/concepts/pydantic.md`` → ``concepts/pydantic``. The bare
    slug is the wiki's addressable identifier; the collection prefix
    (``wiki/``) is stripped and the file extension is dropped.
    """
    pages = [
        PageRead(
            rel_path="wiki/concepts/pydantic.md",
            title="Pydantic",
            spans=[
                Span(
                    heading_path=["H1"],
                    body="hello\n",
                    code_fence=False,
                    start_line=1,
                ),
            ],
            source="wiki",
        ),
    ]
    answer = build_answer_from_pages(question="q", pages=pages, fallback_reason="")
    assert "[[concepts/pydantic]]" in answer.answer
