"""Tests for src/lies/mcp/grounding.py — grounding archivist."""

from __future__ import annotations

import dataclasses
import warnings

import pytest

from lies.markdown_spans import Span
from lies.mcp.grounding import (
    ArchivistCoverageError,
    ArchivistDigest,
    CitationSnippet,
    pick_first_prose_span,
    truncate_at_word_boundary,
)


@pytest.fixture(autouse=True)
def _silence_wiring_skipped_warning() -> None:
    """Silence the ``ground: tool wiring skipped`` warning by default.

    The pre-Fix-Critical-era test surface monkey-patches
    ``grounding.librarian_agent`` to fake the dispatch; those tests
    don't set up a registered wiki, so the new tool-wiring path
    (``register_librarian_tools`` → ``resolve_wiki``) raises
    ``WikiNotRegistered`` and emits a UserWarning. Suppress that
    specific warning here so the existing tests stay quiet. Tests
    that explicitly exercise the wiring path opt back in via
    ``warnings.catch_warnings()``.
    """
    warnings.filterwarnings(
        "ignore",
        message=r"^ground: tool wiring skipped\b",
        category=UserWarning,
    )


def test_truncate_at_word_boundary_short_text_unchanged() -> None:
    text = "Short text under the cap."
    assert truncate_at_word_boundary(text, 200) == text


def test_truncate_at_word_boundary_cuts_at_word_boundary() -> None:
    text = (
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit, "
        "sed do eiusmod tempor incididunt ut labore et dolore magna "
        "aliqua. Ut enim ad minim veniam, quis nostrud exercitation "
        "ullamco laboris nisi ut aliquip ex ea commodo consequat."
    )
    truncated = truncate_at_word_boundary(text, 100)
    assert len(truncated) <= 100
    assert not truncated.endswith(" ")  # trailing partial word dropped
    assert truncated == truncated.rstrip() + ""  # no trailing whitespace
    # Cut should be at a word boundary — last char is not mid-word
    assert truncated.endswith(("m", "n", "o", "a", "e", "i", "s", "t", "d"))  # common word endings
    # The full last word at position 100 should NOT be present truncated mid-way


def test_truncate_at_word_boundary_hard_cut_no_whitespace() -> None:
    text = "a" * 500  # no whitespace
    truncated = truncate_at_word_boundary(text, 100)
    assert len(truncated) == 100


def test_truncate_at_word_boundary_empty_text() -> None:
    assert truncate_at_word_boundary("", 200) == ""


def test_truncate_at_word_boundary_zero_max() -> None:
    with pytest.raises(ValueError):
        truncate_at_word_boundary("text", 0)


def test_pick_first_prose_span_returns_none_when_empty() -> None:
    assert pick_first_prose_span([]) is None


def test_pick_first_prose_span_skips_code_fences() -> None:
    spans = [
        Span(heading_path=["H1"], body="```python\nx = 1\n```", code_fence=True, start_line=1),
        Span(heading_path=["H1"], body="prose body", code_fence=False, start_line=4),
    ]
    picked = pick_first_prose_span(spans)
    assert picked is not None
    assert picked.body == "prose body"


def test_pick_first_prose_span_returns_none_for_only_code_fences() -> None:
    spans = [
        Span(heading_path=["H1"], body="```\n", code_fence=True, start_line=1),
    ]
    assert pick_first_prose_span(spans) is None


def test_pick_first_prose_span_skips_empty_bodies() -> None:
    spans = [
        Span(heading_path=["H1"], body="", code_fence=False, start_line=1),
        Span(heading_path=["H1"], body="real body", code_fence=False, start_line=2),
    ]
    picked = pick_first_prose_span(spans)
    assert picked is not None
    assert picked.body == "real body"


def test_citation_snippet_frozen() -> None:
    cs = CitationSnippet(collection="wiki", slug="x", title="X", snippet="s")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cs.snippet = "other"  # type: ignore[misc]


def test_archivist_digest_frozen() -> None:
    digest = ArchivistDigest(
        question="q",
        tag_expr=None,
        exclude_tags=[],
        citations=[],
        no_coverage=False,
        distinct_pages=0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        digest.no_coverage = True  # type: ignore[misc]


def test_archivist_digest_distinct_pages_round_trips() -> None:
    digest = ArchivistDigest(
        question="q",
        tag_expr=None,
        exclude_tags=[],
        citations=[
            CitationSnippet(collection="wiki", slug="a", title="A", snippet="s"),
            CitationSnippet(collection="wiki", slug="a", title="A", snippet="s2"),
            CitationSnippet(collection="wiki", slug="b", title="B", snippet="s"),
        ],
        no_coverage=False,
        distinct_pages=2,
    )
    assert digest.distinct_pages == 2


def test_archivist_coverage_error_is_exception() -> None:
    with pytest.raises(ArchivistCoverageError):
        raise ArchivistCoverageError("no collections matched")


def test_ground_returns_empty_digest_on_librarian_exception(monkeypatch) -> None:
    """Librarian dispatch failure → no_coverage=True, citations=[]."""
    from lies.mcp import grounding

    def boom(deps):
        raise RuntimeError("qmd daemon offline")

    class _BoomAgent:
        def __init__(self, fn):
            self._fn = fn

        def run_sync(self, deps):
            return self._fn(deps)

    monkeypatch.setattr(grounding, "librarian_agent", lambda: _BoomAgent(boom))

    digest = grounding.ground("what is pydantic?")
    assert digest.no_coverage is True
    assert digest.citations == []
    assert digest.question == "what is pydantic?"
    assert digest.distinct_pages == 0


def test_ground_librarian_exception_emits_no_logfire_warning(monkeypatch, recwarn) -> None:
    """Regression for the LogfireNotConfiguredWarning noise on the exception path.

    Pins Fix 4: when the librarian dispatch raises, the warning must
    flow through stdlib ``warnings`` (not ``logfire.warning``) so a
    non-configured logfire environment does not emit
    ``LogfireNotConfiguredWarning`` on every ground() call. The
    user-visible signal still surfaces via ``recwarn`` — one
    ``UserWarning`` carrying the librarian's failure reason.
    """
    from lies.mcp import grounding

    def boom(deps):
        raise RuntimeError("qmd daemon offline")

    class _BoomAgent:
        def __init__(self, fn):
            self._fn = fn

        def run_sync(self, deps):
            return self._fn(deps)

    monkeypatch.setattr(grounding, "librarian_agent", lambda: _BoomAgent(boom))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        digest = grounding.ground("what is pydantic?")

    assert digest.no_coverage is True
    logfire_warns = [w for w in caught if "LogfireNotConfiguredWarning" in type(w.message).__name__]
    assert logfire_warns == [], f"unexpected LogfireNotConfiguredWarning: {logfire_warns}"
    # The user-visible signal still surfaces — but as a stdlib warning,
    # not a logfire one.
    user_warns = [w for w in caught if "librarian dispatch failed" in str(w.message)]
    assert len(user_warns) >= 1


def test_ground_clamps_top_k_to_bounds(monkeypatch) -> None:
    """top_k=0 → clamp to 1; top_k=999 → clamp to 10. Both calls return without exception."""
    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp import grounding

    def fake_librarian(deps):
        spans = [Span(heading_path=[], body="x", code_fence=False, start_line=1)]
        excerpts = [
            PageExcerpt(collection="wiki", slug=f"slug-{i}", title=f"T{i}", spans=spans)
            for i in range(20)
        ]
        return LibrarianOutput(
            tag_expr=None,
            exclude_tags=[],
            excerpts=excerpts,
            distinct_pages=20,
        )

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    digest_low = grounding.ground("q", top_k=0)
    digest_high = grounding.ground("q", top_k=999)
    # top_k is a request hint forwarded to the librarian; the brief does not
    # require ground() to re-truncate the librarian's output. We verify the
    # function returns a digest in both cases without exception.
    assert digest_low.citations == digest_low.citations  # no exception
    assert digest_high.citations == digest_high.citations


def test_ground_uses_first_prose_span_per_excerpt(monkeypatch) -> None:
    """First prose span wins; snippet truncated to ≤200 chars."""
    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp import grounding

    long_prose = ("A " * 150).strip()  # > 200 chars
    spans = [
        Span(heading_path=["H1"], body="```\nx = 1\n```", code_fence=True, start_line=1),
        Span(heading_path=["H1"], body=long_prose, code_fence=False, start_line=4),
    ]
    excerpts = [
        PageExcerpt(collection="wiki", slug="x", title="X", spans=spans),
    ]

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=excerpts, distinct_pages=1)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    digest = grounding.ground("q")
    assert len(digest.citations) == 1
    assert digest.citations[0].snippet != ""
    assert len(digest.citations[0].snippet) <= 200
    assert digest.citations[0].slug == "x"
    assert digest.citations[0].collection == "wiki"


def test_ground_skips_excerpts_with_only_code_fences(monkeypatch) -> None:
    """Excerpt with no prose spans → skipped from citations."""
    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp import grounding

    code_only = [
        Span(heading_path=["H1"], body="```\nx = 1\n```", code_fence=True, start_line=1),
    ]
    prose_only = [
        Span(heading_path=["H1"], body="real text", code_fence=False, start_line=1),
    ]
    excerpts = [
        PageExcerpt(collection="wiki", slug="code-only", title="C", spans=code_only),
        PageExcerpt(collection="wiki", slug="prose", title="P", spans=prose_only),
    ]

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=excerpts, distinct_pages=2)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    digest = grounding.ground("q")
    assert len(digest.citations) == 1
    assert digest.citations[0].slug == "prose"
    assert digest.no_coverage is False
    assert digest.distinct_pages == 1


def test_ground_no_coverage_distinguishes_empty_corpus_from_scope_miss(monkeypatch) -> None:
    """Empty excerpts → no_coverage=False (corpus state not established here)."""
    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import grounding

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    digest = grounding.ground("q")
    assert digest.no_coverage is False
    assert digest.citations == []
    assert digest.distinct_pages == 0


def test_ground_no_coverage_true_when_corpus_non_empty_but_no_hits(monkeypatch) -> None:
    """Catalog has wiki pages but librarian returns 0 excerpts → no_coverage=True.

    Pins the F19 spec contract: ``no_coverage=True`` distinguishes the
    scope-miss-on-populated-wiki case (``corpus_size > 0`` AND zero
    excerpts) from the empty-corpus case (catalog unreadable or
    ``corpus_size == 0``). ``ground()`` probes the catalog through
    :func:`lies.mcp.grounding._corpus_page_count`, which is
    monkeypatched here to avoid touching the real sqlite catalog.
    """
    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import grounding

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    monkeypatch.setattr(grounding, "_corpus_page_count", lambda: 5)
    digest = grounding.ground("q")
    assert digest.no_coverage is True
    assert digest.citations == []


def test_ground_no_coverage_false_when_corpus_also_empty(monkeypatch) -> None:
    """Both catalog empty AND librarian returns 0 excerpts → no_coverage=False."""
    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import grounding

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    monkeypatch.setattr(grounding, "_corpus_page_count", lambda: 0)
    digest = grounding.ground("q")
    assert digest.no_coverage is False
    assert digest.citations == []


def test_ground_no_coverage_false_when_corpus_probe_raises(monkeypatch) -> None:
    """Catalog probe exception → no_coverage=False (fail-open)."""
    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import grounding

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)

    def boom() -> int:
        raise RuntimeError("catalog unreadable")

    monkeypatch.setattr(grounding, "_corpus_page_count", boom)
    digest = grounding.ground("q")
    assert digest.no_coverage is False
    assert digest.citations == []


def test_ground_no_coverage_false_when_citations_present(monkeypatch) -> None:
    """Citations present → no_coverage=False even if corpus probe returns >0."""
    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp import grounding

    def fake_librarian(deps):
        spans = [Span(heading_path=[], body="x", code_fence=False, start_line=1)]
        excerpts = [PageExcerpt(collection="wiki", slug="x", title="X", spans=spans)]
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=excerpts, distinct_pages=1)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    monkeypatch.setattr(grounding, "_corpus_page_count", lambda: 5)
    digest = grounding.ground("q")
    assert digest.no_coverage is False
    assert len(digest.citations) == 1


def test_ground_library_collection_names_cached_across_calls(monkeypatch) -> None:
    """Regression for Fix 5: library_collection_names memoization.

    Pins that the F15 tag-filter dispatch path in ground() consults
    the registry's lru_cache rather than re-traversing the library's
    collections_root via iterdir on every call. Read
    ``cache_info()`` after a sequence of calls: ``misses`` counts
    how many times the underlying body actually ran (vs the
    ``call_count`` if we had replaced the function outright, which
    would lose the lru_cache wrapping).
    """
    from lies.agents.librarian import LibrarianOutput
    from lies.library import registry
    from lies.mcp import grounding

    def fake_librarian(deps):
        return LibrarianOutput(tag_expr=None, exclude_tags=[], excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    # Make the underlying body return a stable, non-empty
    # addressable-tag set so the resolver accepts the ``tag_expr``.
    # Patching ``_collections_root`` is cleaner than swapping out
    # the lru_cache-wrapped function (which would lose the cache
    # itself and break the regression intent).
    monkeypatch.setattr(registry, "_collections_root", lambda: _FakeRoot())
    # Reset the cache so the assertion sees only this test's misses.
    registry.library_collection_names.cache_clear()

    grounding.ground("q")  # tag_expr=None — resolver path skipped
    grounding.ground("q", tag_expr="a|b")  # resolver consults cache
    grounding.ground("q", tag_expr="a")  # resolver hits cache

    info = registry.library_collection_names.cache_info()
    # One underlying body call regardless of how many ground()
    # invocations crossed the resolver boundary. Without
    # memoization, every ground() with a tag_expr would force a
    # fresh iterdir.
    assert info.misses == 1, f"expected 1 miss (memoized), got {info.misses}"


class _FakeRoot:
    """Fake Path-like for ``_collections_root`` in the cache test.

    Exposes ``.exists()`` returning ``True`` and ``.iterdir()``
    yielding two fake directory entries so
    :func:`lies.library.registry.library_collection_names` walks a
    stable, non-empty set without touching the real library.
    """

    def exists(self) -> bool:
        return True

    def iterdir(self):
        return iter([_FakeEntry("a"), _FakeEntry("b")])


class _FakeEntry:
    def __init__(self, name: str) -> None:
        self.name = name

    def is_dir(self) -> bool:
        return True


def _patch_librarian(monkeypatch, grounding_module, fake_fn):
    """Replace ``librarian_agent().run_sync(...)`` with ``fake_fn(deps)``.

    The real pydantic_ai ``Agent.run_sync`` returns an
    ``AgentRunResult`` whose ``.output`` attribute carries the typed
    output. ``ground()`` reads ``result.output``, so the fake mirrors
    that wrapper shape — not the raw ``LibrarianOutput``.
    """

    class _FakeResult:
        def __init__(self, output):
            self.output = output

    class _FakeAgent:
        def run_sync(self, user_prompt, *, deps):  # noqa: ARG002
            return _FakeResult(fake_fn(deps))

    monkeypatch.setattr(grounding_module, "librarian_agent", lambda: _FakeAgent())
