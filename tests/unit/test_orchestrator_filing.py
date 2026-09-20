"""Tests for orchestrator filing-back helpers (F19)."""

from __future__ import annotations

from lies.agents.librarian import LibrarianOutput, PageExcerpt
from lies.agents.query_synthesizer import QueryAnswer
from lies.markdown_spans import Span
from lies.orchestrator import (
    _format_heading_path,
    _render_evidence,
    _should_file,
    _slug_from_path,
)


def test_format_heading_path_empty() -> None:
    assert _format_heading_path(None) == "(top of page)"
    assert _format_heading_path([]) == "(top of page)"


def test_format_heading_path_joins() -> None:
    assert _format_heading_path(["H1", "H2"]) == "H1 > H2"


def test_slug_from_path_strips_collection_prefix() -> None:
    assert _slug_from_path("wiki/concepts/pydantic.md") == "concepts/pydantic"
    assert _slug_from_path("claude_platform/concepts/alpha.md") == "concepts/alpha"
    assert _slug_from_path("no-slash") == "no-slash"


def test_render_evidence_emits_span_heading_inline() -> None:
    from lies.query.citation import Citation, ClaimCitation

    citations = [
        Citation(
            path="wiki/concepts/pydantic.md",
            source="wiki",
            heading_path=["Definition", "Nested Models"],
        ),
        Citation(
            path="claude_platform/concepts/alpha.md",
            source="library",
            heading_path=["Core API"],
        ),
    ]
    ccs = [
        ClaimCitation(
            claim="Nested models are validated recursively.",
            citation_index=0,
            quote="Nested models are validated recursively.",
        ),
        ClaimCitation(
            claim="A Session is the gateway.",
            citation_index=1,
            quote="A Session is the gateway to the database.",
        ),
    ]
    evidence = _render_evidence(citations, ccs)
    assert (
        '[[concepts/pydantic]] (Definition > Nested Models): "Nested models are validated recursively."'
        in evidence
    )
    assert '[[concepts/alpha]] (Core API): "A Session is the gateway to the database."' in evidence


# --- _should_file ----------------------------------------------------------
#
# The ``_should_file`` gate is module-level (catalog check lives on the
# real ``Orchestrator`` instance; see ``Orchestrator._should_file``).
# The test wrappers below instantiate a minimal shim that pairs the
# catalog check stub with the gate via staticmethod-style binding so we
# exercise the gate logic without instantiating ``Orchestrator``.


class _FilingGateShim:
    """Minimal stand-in for ``Orchestrator._should_file``.

    Binds ``_should_file`` as a staticmethod on the class so the test
    calls ``shim._should_file(answer, lo)`` with no ``self``, mirroring
    the shape an ``Orchestrator`` instance exposes. ``_should_file``
    must be visible from the class body, so the import lives at the
    top of this module — class-body scope cannot read enclosing
    function locals.
    """

    def _has_existing_concept_page(self, answer: QueryAnswer) -> bool:  # noqa: ARG002
        return False

    _should_file = staticmethod(_should_file)


def test_should_file_requires_two_distinct_pages() -> None:
    spans = [Span(heading_path=[], body="b", code_fence=False, start_line=1)]
    lo_one_page = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[PageExcerpt(collection="wiki", slug="a", title="A", spans=spans)],
        distinct_pages=1,
    )
    lo_two_pages = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[
            PageExcerpt(collection="wiki", slug="a", title="A", spans=spans),
            PageExcerpt(collection="wiki", slug="b", title="B", spans=spans),
        ],
        distinct_pages=2,
    )
    answer = QueryAnswer(
        answer=(
            "Substantive answer body that spans multiple lines and "
            "paragraphs.\n\n"
            "The first excerpt covers the alpha concept.\n\n"
            "The second excerpt grounds the beta concept.\n\n"
            "Three non-blank lines satisfy the gate's substantive heuristic."
        ),
        citations=[],
        should_file=True,
        format_hint="md",
    )
    orch = _FilingGateShim()
    assert orch._should_file(answer, lo_one_page) is False
    assert orch._should_file(answer, lo_two_pages) is True


def test_should_file_drops_one_liner() -> None:
    spans = [Span(heading_path=[], body="b", code_fence=False, start_line=1)]
    lo = LibrarianOutput(
        tag_expr=None,
        exclude_tags=[],
        excerpts=[
            PageExcerpt(collection="wiki", slug="a", title="A", spans=spans),
            PageExcerpt(collection="wiki", slug="b", title="B", spans=spans),
        ],
        distinct_pages=2,
    )
    answer_one_liner = QueryAnswer(
        answer="yes",
        citations=[],
        should_file=True,
        format_hint="md",
    )
    answer_substantive = QueryAnswer(
        answer=(
            "Long enough answer body to pass the substantive threshold "
            "for filing-back.\n\n"
            "It covers the second concept and references a few "
            "additional contexts so the one-liner heuristic returns False.\n\n"
            "Three non-blank lines is the gate, and this body has them."
        ),
        citations=[],
        should_file=True,
        format_hint="md",
    )
    orch = _FilingGateShim()
    assert orch._should_file(answer_one_liner, lo) is False
    assert orch._should_file(answer_substantive, lo) is True
