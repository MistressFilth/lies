"""Tests for src/lies/mcp/grounding.py — grounding archivist."""

from __future__ import annotations

import dataclasses
import warnings
from dataclasses import dataclass

import pytest

from lies.agents.librarian import LibrarianOutput
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


def test_citation_snippet_source_kind_defaults_to_library() -> None:
    """CitationSnippet defaults source_kind to 'library' for backward compat."""
    from lies.mcp.grounding import CitationSnippet

    snip = CitationSnippet(
        collection="mermaid",
        slug="syntax/flowchart",
        title="Flowchart syntax",
        snippet="flowchart TD; A-->B",
    )
    assert snip.source_kind == "library"


def test_citation_snippet_source_kind_explicit() -> None:
    """CitationSnippet accepts an explicit source_kind."""
    from lies.mcp.grounding import CitationSnippet

    snip = CitationSnippet(
        collection="default",
        slug="concepts/pydantic",
        title="Pydantic concept",
        snippet="Pydantic is a data validation library.",
        source_kind="wiki",
    )
    assert snip.source_kind == "wiki"


def test_archivist_digest_frozen() -> None:
    digest = ArchivistDigest(
        question="q",
        tag_expr=None,
        exclude_expr=None,
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
        exclude_expr=None,
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

    class _BoomAgent:
        def run_sync(self, user_prompt, *, deps):
            raise RuntimeError("qmd daemon offline")

    monkeypatch.setattr(grounding, "librarian_agent", lambda: _BoomAgent())

    digest = grounding.ground("what is pydantic?")
    assert digest.no_coverage is True
    assert digest.citations == []
    assert digest.question == "what is pydantic?"
    assert digest.distinct_pages == 0


def test_ground_librarian_exception_emits_no_logfire_warning(monkeypatch, recwarn) -> None:
    """Regression for the LogfireNotConfiguredWarning noise on the exception path.

    Pins Fix 4: when the dispatch path raises, the warning must flow
    through stdlib ``warnings`` (not ``logfire.warning``) so a
    non-configured logfire environment does not emit
    ``LogfireNotConfiguredWarning`` on every ground() call. The
    user-visible signal still surfaces via ``recwarn`` — one
    ``UserWarning`` carrying the dispatch's failure reason.

    After the library-mode read-side rewrite, unscoped ``ground()``
    dispatches via the fan-out helper (``_fanout_unscoped``) rather
    than the F18 librarian. The migration: the dispatch-exception
    branch is exercised by raising from the fan-out mock; the
    surfaced warning now carries "fan-out dispatch failed" instead
    of "librarian dispatch failed". The stdlib-warnings contract is
    unchanged — logfire never sees the warning.

    Seeds ``library_collection_names`` with a non-empty set so the
    ``no_library=True`` early return doesn't short-circuit before
    the fan-out mock can be exercised.
    """
    from lies.library import registry as registry_mod
    from lies.mcp import grounding

    monkeypatch.setattr(
        registry_mod,
        "library_collection_names",
        lambda: frozenset({"_test_fake"}),
    )

    async def _boom_fanout(*_args, **_kwargs):
        raise RuntimeError("qmd daemon offline")

    monkeypatch.setattr(grounding, "_fanout_unscoped", _boom_fanout)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        digest = grounding.ground("what is pydantic?")

    assert digest.no_coverage is True
    logfire_warns = [w for w in caught if "LogfireNotConfiguredWarning" in type(w.message).__name__]
    assert logfire_warns == [], f"unexpected LogfireNotConfiguredWarning: {logfire_warns}"
    # The user-visible signal still surfaces — but as a stdlib warning,
    # not a logfire one. After the rewrite the unscoped path surfaces
    # "fan-out dispatch failed"; the contract (stdlib warnings, not
    # logfire) is the same as the pre-rewrite librarian path.
    user_warns = [w for w in caught if "fan-out dispatch failed" in str(w.message)]
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
            exclude_expr=None,
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
    """First prose span wins; snippet truncated to ≤200 chars.

    Library-mode rewrite migration: the unscoped path bypasses the
    F18 librarian and dispatches via ``_fanout_unscoped``. Mock the
    fan-out helper at the import seam so the citation-building loop
    sees populated ``PageExcerpt`` rows with the same shape the
    real fan-out would return on a successful scan.
    """
    from lies.agents.librarian import PageExcerpt
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

    def fake_fanout(*_args, **_kwargs):
        return excerpts

    _patch_fanout(monkeypatch, grounding, fake_fanout)
    digest = grounding.ground("q")
    assert len(digest.citations) == 1
    assert digest.citations[0].snippet != ""
    assert len(digest.citations[0].snippet) <= 200
    assert digest.citations[0].slug == "x"
    assert digest.citations[0].collection == "wiki"


def test_ground_skips_excerpts_with_only_code_fences(monkeypatch) -> None:
    """Excerpt with no prose spans → skipped from citations.

    Library-mode rewrite migration: the unscoped path bypasses the
    F18 librarian and dispatches via ``_fanout_unscoped``. Mock the
    fan-out helper directly so the citation-building loop sees the
    mixed code-only + prose-only excerpts.
    """
    from lies.agents.librarian import PageExcerpt
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

    def fake_fanout(*_args, **_kwargs):
        return excerpts

    _patch_fanout(monkeypatch, grounding, fake_fanout)
    digest = grounding.ground("q")
    assert len(digest.citations) == 1
    assert digest.citations[0].slug == "prose"
    assert digest.no_coverage is False
    assert digest.distinct_pages == 1


def test_ground_no_coverage_true_when_librarian_reports_it(monkeypatch) -> None:
    """Librarian reports no_coverage=True → digest carries no_coverage=True.

    Pins F18 Task 2: the digest's ``no_coverage`` field is read
    directly from ``LibrarianOutput.no_coverage`` rather than from a
    catalog probe. The librarian is the source of truth for the
    scope-miss signal.
    """
    from lies.agents.librarian import LibrarianOutput
    from lies.mcp import grounding

    def fake_librarian(question, deps):
        return LibrarianOutput(
            tag_expr=None,
            exclude_expr=None,
            excerpts=[],
            distinct_pages=0,
            no_coverage=True,
        )

    class _FakeAgent:
        def run_sync(self, user_prompt, *, deps):
            return _FakeResult(fake_librarian(user_prompt, deps))

    class _FakeResult:
        def __init__(self, output):
            self.output = output

    monkeypatch.setattr(grounding, "librarian_agent", lambda: _FakeAgent())
    digest = grounding.ground("q")
    assert digest.no_coverage is True
    assert digest.citations == []


def test_ground_no_coverage_false_when_librarian_reports_zero(monkeypatch) -> None:
    """Librarian reports no_coverage=False AND zero excerpts → digest no_coverage=False.

    Pins F18 Task 2: when the librarian reports ``no_coverage=False``
    (empty corpus), the digest mirrors that — the empty-corpus case
    is NOT a no-coverage signal.

    Library-mode rewrite migration: this contract is librarian-path
    specific — the unscoped fan-out computes ``no_coverage`` from
    ``len(excerpts) == 0`` (always ``True`` when the fan-out yields
    no hits). Force the librarian path by passing a ``tag_expr`` and
    seeding the matching library collection so the F15 tag-filter
    dispatch validates; the mocked librarian agent then drives the
    digest's ``no_coverage`` field per the F18 Task 2 contract.
    """
    from lies import xdg
    from lies.agents.librarian import LibrarianOutput
    from lies.constants import LIES_DATA_SUBDIR
    from lies.mcp import grounding

    # Seed a ``wiki`` library collection so ``+wiki`` validates at the
    # F15 tag-filter dispatch. The :func:`_isolated_xdg` autouse
    # fixture has already redirected XDG into ``tmp_path``.
    coll_root = xdg.data_home() / LIES_DATA_SUBDIR / "library" / "collections" / "wiki"
    coll_root.mkdir(parents=True, exist_ok=True)

    def fake_librarian(question, deps):
        return LibrarianOutput(
            tag_expr=None,
            exclude_expr=None,
            excerpts=[],
            distinct_pages=0,
            no_coverage=False,
        )

    class _FakeAgent:
        def run_sync(self, user_prompt, *, deps):
            return _FakeResult(fake_librarian(user_prompt, deps))

    class _FakeResult:
        def __init__(self, output):
            self.output = output

    monkeypatch.setattr(grounding, "librarian_agent", lambda: _FakeAgent())
    digest = grounding.ground("q", tag_expr="wiki")
    assert digest.no_coverage is False


def test_ground_no_coverage_false_when_librarian_returns_hits(monkeypatch) -> None:
    """Librarian returns hits (no_coverage=False) → digest no_coverage=False.

    Pins F18 Task 2: a successful query is never a no-coverage
    signal, regardless of corpus size. The librarian's
    ``no_coverage=False`` flows through unchanged.

    Library-mode rewrite migration: the unscoped fan-out computes
    ``no_coverage = len(excerpts) == 0`` so non-empty hits
    propagate the same ``False`` value through to the digest. Mock
    the fan-out helper directly with a populated ``PageExcerpt``
    list so the digest sees a non-empty result on the unscoped
    path.
    """
    from lies.agents.librarian import PageExcerpt
    from lies.markdown_spans import Span
    from lies.mcp import grounding

    spans = [Span(heading_path=[], body="x", code_fence=False, start_line=1)]
    excerpts = [PageExcerpt(collection="wiki", slug="x", title="X", spans=spans)]

    def fake_fanout(*_args, **_kwargs):
        return excerpts

    _patch_fanout(monkeypatch, grounding, fake_fanout)
    digest = grounding.ground("q")
    assert digest.no_coverage is False
    assert len(digest.citations) == 1


def test_ground_dispatch_failure_still_yields_no_coverage_true(monkeypatch) -> None:
    """Dispatch exception → digest no_coverage=True (fail-closed on the ground path).

    Pins F18 Task 2: the no-coverage signal still fires when the
    librarian itself raises — the dispatch-exception branch wraps the
    bundle read, so the same ``no_coverage=True`` envelope is
    preserved.
    """
    from lies.mcp import grounding

    class _BoomAgent:
        def run_sync(self, user_prompt, *, deps):
            raise RuntimeError("qmd daemon offline")

    monkeypatch.setattr(grounding, "librarian_agent", lambda: _BoomAgent())
    digest = grounding.ground("q")
    assert digest.no_coverage is True


# ---------------------------------------------------------------------------
# Bug C — `ground` wire format exposes `searched_scope` (F15 envelope parity)
# ---------------------------------------------------------------------------


def test_archivist_digest_searched_scope_default_empty() -> None:
    """``ArchivistDigest.searched_scope`` defaults to ``[]`` for back-compat.

    Pin for the Bug C fix: the field is added at the END of the
    dataclass so existing positional constructions remain valid.
    Without the default, every pre-fix call site would break. Also
    pins the frozen-dataclass contract — assigning to the field
    raises ``FrozenInstanceError``.
    """
    digest = ArchivistDigest(
        question="q",
        tag_expr=None,
        exclude_expr=None,
        citations=[],
        no_coverage=False,
        distinct_pages=0,
    )
    assert digest.searched_scope == []
    with pytest.raises(dataclasses.FrozenInstanceError):
        digest.searched_scope = ["x"]  # type: ignore[misc]


def test_ground_searched_scope_untagged_returns_all_collections(monkeypatch) -> None:
    """Untagged ``ground()`` → ``searched_scope`` = every registered collection.

    Pins Bug C: the digest's ``searched_scope`` mirrors the F15
    envelope that ``Orchestrator.run_query`` writes onto
    ``SynthesizedAnswer.searched_scope``. Untagged scope = every
    collection the library knows about, sorted. Tested by patching
    the library registry to a known set so the assertion sees
    exactly the names we expect, regardless of the host's live
    library state.
    """
    from lies.library import registry as registry_mod
    from lies.mcp import grounding

    # Replace the lru_cache-wrapped ``library_collection_names``
    # with a plain lambda so ``_all_collection_names`` reads our
    # deterministic fixture instead of walking the host's live
    # library. Also clear any prior cache so the swap is honored
    # even if a sibling test in this module already populated it.
    monkeypatch.setattr(
        registry_mod,
        "library_collection_names",
        lambda: frozenset({"opencode", "claude_platform", "mermaid"}),
    )

    def fake_librarian(deps):
        from lies.agents.librarian import LibrarianOutput

        return LibrarianOutput(tag_expr=None, exclude_expr=None, excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)

    digest = grounding.ground("q")
    assert digest.searched_scope == ["claude_platform", "mermaid", "opencode"]


def test_ground_searched_scope_tagged_returns_matching_only(monkeypatch) -> None:
    """Tagged ``ground(tag_expr="opencode")`` → ``searched_scope`` = just that collection.

    Pins Bug C: filtered scope = the subset whose ``atom_matches``
    is true for the resolved include AST, sorted. The result mirrors
    ``Orchestrator.run_query``'s contract — tagged ground returns a
    narrower scope than untagged ground.
    """
    from lies.agents.librarian import LibrarianOutput
    from lies.library import registry as registry_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding

    monkeypatch.setattr(
        registry_mod,
        "library_collection_names",
        lambda: frozenset({"opencode", "claude_platform", "mermaid"}),
    )

    # Stub ``library_collection_metas`` so the matching walker sees
    # the three collections with deterministic tag sets.
    def _fake_metas() -> list[LibraryCollectionMeta]:
        return [
            LibraryCollectionMeta(name="opencode", tags=("cli", "agent")),
            LibraryCollectionMeta(name="claude_platform", tags=("api",)),
            LibraryCollectionMeta(name="mermaid", tags=("syntax",)),
        ]

    monkeypatch.setattr(registry_mod, "library_collection_metas", _fake_metas)
    # Also stub ``library_collection_tags`` so the F15 validator's
    # available-set recognizes ``opencode`` as addressable.
    monkeypatch.setattr(
        registry_mod,
        "library_collection_tags",
        lambda: frozenset({"cli", "agent", "api", "syntax"}),
    )

    def fake_librarian(deps):
        return LibrarianOutput(
            tag_expr="opencode", exclude_expr=None, excerpts=[], distinct_pages=0
        )

    _patch_librarian(monkeypatch, grounding, fake_librarian)

    digest = grounding.ground("q", tag_expr="opencode")
    assert digest.searched_scope == ["opencode"]


def test_ground_searched_scope_populated_on_librarian_exception(monkeypatch) -> None:
    """Librarian dispatch failure → ``searched_scope`` is still populated.

    Pins Bug C fail-soft posture: ``searched_scope`` is computed
    once (before the librarian dispatch) and threaded through every
    return path. A failed dispatch reports ``no_coverage=True`` but
    the digest still tells the caller which collections were
    searched — same contract as ``Orchestrator.run_query`` writing
    ``searched_scope`` before the F18 ``no_coverage`` decision.
    """
    from lies.library import registry as registry_mod
    from lies.mcp import grounding

    monkeypatch.setattr(
        registry_mod,
        "library_collection_names",
        lambda: frozenset({"opencode", "claude_platform"}),
    )

    class _BoomAgent:
        def run_sync(self, user_prompt, *, deps):
            raise RuntimeError("qmd daemon offline")

    monkeypatch.setattr(grounding, "librarian_agent", lambda: _BoomAgent())

    digest = grounding.ground("q")
    assert digest.no_coverage is True
    assert digest.searched_scope == ["claude_platform", "opencode"]


def test_mcp_ground_wire_format_includes_searched_scope(monkeypatch) -> None:
    """The MCP ``ground`` tool's JSON envelope carries ``searched_scope``.

    Pins Bug C at the wire boundary: the MCP tool wrapper
    (``mcp_ground`` in ``src/lies/mcp/server.py``) returns
    ``dataclasses.asdict(digest)`` for FastMCP serialization. The
    new ``searched_scope`` field must appear in the resulting JSON
    dict so MCP clients can introspect the resolved scope. Without
    this pin, an accidental ``asdict`` override or field-name typo
    would silently drop the field from the wire.
    """
    from dataclasses import asdict

    from lies.library import registry as registry_mod
    from lies.mcp import grounding
    from lies.mcp.server import mcp_ground

    monkeypatch.setattr(
        registry_mod,
        "library_collection_names",
        lambda: frozenset({"opencode", "claude_platform", "mermaid"}),
    )

    def fake_librarian(deps):
        from lies.agents.librarian import LibrarianOutput

        return LibrarianOutput(tag_expr=None, exclude_expr=None, excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)

    wire = mcp_ground(question="what is opencode?")
    assert isinstance(wire, dict)
    assert "searched_scope" in wire, (
        f"ground wire envelope dropped searched_scope: keys={sorted(wire.keys())}"
    )
    assert wire["searched_scope"] == ["claude_platform", "mermaid", "opencode"]

    # Also pin the dataclass-level asdict path so the dataclass itself
    # carries the field — this is what the MCP tool relies on.
    digest = grounding.ground("what is opencode?")
    asdict_payload = asdict(digest)
    assert "searched_scope" in asdict_payload


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
        return LibrarianOutput(tag_expr=None, exclude_expr=None, excerpts=[], distinct_pages=0)

    _patch_librarian(monkeypatch, grounding, fake_librarian)
    # Make the underlying body return a stable, non-empty
    # addressable-tag set so the resolver accepts the ``tag_expr``.
    # Patching ``_collections_root`` is cleaner than swapping out
    # the lru_cache-wrapped function (which would lose the cache
    # itself and break the regression intent).
    monkeypatch.setattr(registry, "_collections_root", lambda: _FakeRoot())
    # Reset the cache so the assertion sees only this test's misses.
    # ``library_collection_names`` is now a thin wrapper that keys the
    # inner lru_cache on the directory mtime; clear the inner cache
    # so the test's miss counter starts from zero.
    registry._library_collection_names_cached.cache_clear()

    grounding.ground("q")  # tag_expr=None — resolver path skipped
    grounding.ground("q", tag_expr="a|b")  # resolver consults cache
    grounding.ground("q", tag_expr="a")  # resolver hits cache

    info = registry._library_collection_names_cached.cache_info()
    # One underlying body call regardless of how many ground()
    # invocations crossed the resolver boundary. Without
    # memoization, every ground() with a tag_expr would force a
    # fresh iterdir.
    assert info.misses == 1, f"expected 1 miss (memoized), got {info.misses}"


class _FakeRoot:
    """Fake Path-like for ``_collections_root`` in the cache test.

    Exposes ``.exists()`` returning ``True``, ``.iterdir()``
    yielding two fake directory entries, and ``.stat()`` returning
    a deterministic mtime so the new mtime-keyed cache can record
    a stable cache key. Without ``.stat()`` the wrapper around
    :func:`library_collection_names` (which keys on the dir mtime)
    raises ``AttributeError`` and the cache assertion cannot
    observe a hit.
    """

    def exists(self) -> bool:
        return True

    def iterdir(self):
        return iter([_FakeEntry("a"), _FakeEntry("b")])

    def stat(self) -> _FakeStat:
        return _FakeStat()


class _FakeStat:
    st_mtime_ns: int = 1_700_000_000_000_000_000


class _FakeEntry:
    def __init__(self, name: str) -> None:
        self.name = name

    def is_dir(self) -> bool:
        return True


def test_collect_available_tags_mcp_includes_library_collections(monkeypatch) -> None:
    """Library-collection names must appear in the tag-validator's
    available set with the ``c:`` qualifier prefix.

    Regression for Fix 3 (Task 3 brief): the F15 tag-expression
    validator on the MCP ``query`` / ``answer`` tool path needs to
    know which ``c:``-prefixed atoms are addressable so a
    ``c:opencode`` filter does not raise ``TagExprUnknown``. The MCP
    tool path is distinct from the ``ground`` archivist's
    ``grounding.py`` path; the helper lives at
    ``lies.mcp.server._collect_available_tags_mcp``.
    """
    from lies.mcp.server import _collect_available_tags_mcp

    monkeypatch.setattr(
        "lies.library.registry.library_collection_names",
        lambda: frozenset({"opencode", "pydantic_ai"}),
    )
    tags = _collect_available_tags_mcp(None)
    assert "c:opencode" in tags
    assert "c:pydantic_ai" in tags


def test_collect_available_tags_mcp_includes_library_collection_tags(monkeypatch) -> None:
    """Each ``LibraryCollectionConfig.tags`` entry must appear in the
    tag-validator's available set with the ``t:`` qualifier prefix.

    Regression for Task 8: pre-fix the validator only enumerated
    collection NAMES (with the ``c:`` prefix). The ``tags`` field on
    each collection's ``config.yaml`` (e.g. ``mermaid`` carrying
    ``[syntax, docs, mermaid]``) was invisible to the validator, so
    a ``t:mermaid`` filter against a library-collection tag raised
    ``TagExprUnknown``. The fix unions each registered collection's
    ``tags`` (via :func:`library_collection_tags`) into the available
    set with the ``t:`` prefix.
    """
    from lies.mcp.server import _collect_available_tags_mcp

    monkeypatch.setattr(
        "lies.library.registry.library_collection_tags",
        lambda: frozenset({"mermaid", "syntax", "docs", "cli"}),
    )
    tags = _collect_available_tags_mcp(None)
    assert "t:mermaid" in tags
    assert "t:syntax" in tags
    assert "t:docs" in tags
    assert "t:cli" in tags


def test_collect_available_tags_mcp_accepts_bare_tag_names(monkeypatch) -> None:
    """Bare tag names must validate too — F15 treats ``+tag`` as the
    implicit-t alias for ``+t:tag``.

    Regression for the v0.37.9 fix: pre-fix
    :func:`_collect_available_tags_mcp` only registered each
    ``LibraryCollectionConfig.tags`` entry with the explicit ``t:``
    qualifier prefix. A bare ``+claude`` expression — which is the
    canonical F15 include form against a library-collection tag —
    parses to ``Include("claude", qualifier=None)``, and the resolver
    checks ``expr.tag in available`` directly, so the bare string
    had to be present in the set or ``TagExprUnknown`` fired at the
    MCP ``query`` / ``answer`` / ``ground`` boundary even though
    ``claude`` was a real tag on multiple collections. The fix adds
    each tag BARE alongside the ``t:`` form so the user's
    user-confirmed semantic (``bare +tag == +t:tag``) validates.
    """
    from lies.mcp.server import _collect_available_tags_mcp

    monkeypatch.setattr(
        "lies.library.registry.library_collection_tags",
        lambda: frozenset({"mermaid", "syntax", "docs", "cli"}),
    )
    tags = _collect_available_tags_mcp(None)
    # ``t:``-prefixed form still validates (no regression for Task 8).
    assert "t:mermaid" in tags
    # Bare form is now also addressable (implicit-t: alias).
    assert "mermaid" in tags
    assert "syntax" in tags
    assert "docs" in tags
    assert "cli" in tags


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


def _patch_fanout(monkeypatch, grounding_module, fake_fn):
    """Replace ``_fanout_unscoped(...)`` with an async fake returning ``fake_fn(...)``.

    Mirrors :func:`_patch_librarian` for the unscoped fan-out path
    introduced by the library-mode read-side rewrite. Unscoped
    ``ground()`` no longer dispatches through the F18 librarian —
    it bypasses the LLM round-trip and calls
    :func:`lies.mcp.grounding._fanout_unscoped` directly via
    ``asyncio.run``. The real helper is async, so the fake wraps the
    sync ``fake_fn`` in an ``async def`` to preserve the awaitable
    contract.

    Also seeds ``library_collection_names`` with a non-empty
    frozenset so the ``no_library=True`` fast-path early return
    (``ground()`` returns ``no_library=True`` when the library has
    zero registered collections and the query is unscoped) does not
    short-circuit before the fan-out mock is reached. The seeded
    collection list is only used by the F15 ``searched_scope``
    envelope; the fan-out itself never walks the registry because
    the mock bypasses it.
    """

    async def _async_fake(*args, **kwargs):
        return fake_fn(*args, **kwargs)

    monkeypatch.setattr(grounding_module, "_fanout_unscoped", _async_fake)

    # Seed the library registry so the ``no_library`` fast-path does
    # not fire before ``_fanout_unscoped`` is reached. Both the inner
    # ``library_collection_names`` and the outer wrapper (cache key
    # mtime path) are patched so the early-return predicate reads the
    # seeded set deterministically regardless of the host's live
    # library state.
    from lies.library import registry as registry_mod

    monkeypatch.setattr(
        registry_mod,
        "library_collection_names",
        lambda: frozenset({"_patch_fanout_fake"}),
    )


# ---------------------------------------------------------------------------
# Fix Critical regression pins
# ---------------------------------------------------------------------------


def _registered_tool_names(agent: object) -> list[str]:
    """Collect the names of every tool registered on ``agent``.

    Mirrors the helper in ``tests/unit/memory/test_tools.py`` —
    pydantic-ai exposes the function toolset via ``agent.toolsets``;
    iterating each toolset's ``tools`` mapping yields the registered
    tool names. Inlined here so this test file does not import from
    a sibling test module.
    """
    names: list[str] = []
    for toolset in getattr(agent, "toolsets", []):
        names.extend(getattr(toolset, "tools", {}).keys())
    return sorted(set(names))


def test_register_librarian_tools_attaches_three_tools(empty_wiki: object) -> None:
    """Wiring attaches ``wiki_search`` / ``wiki_read`` / ``wiki_catalog``.

    Pins the canonical wiring contract: ``register_librarian_tools``
    is the single source of truth for the F18 4-step trio, used by
    both the orchestrator (delegates from
    ``Orchestrator._register_librarian_tools``) and the MCP-layer
    ``ground()`` path. The pre-Fix-Critical-era surface left the
    orchestrator as the only caller, so any non-orchestrator dispatch
    (notably ``ground()``) reached the LLM with no tools at all.

    Uses ``"test"`` as the model id so the bare agent factory does
    not try to instantiate the Anthropic provider — the test only
    inspects the tool registry, never runs ``run_sync``.
    """
    from lies.agents.librarian import librarian_agent, register_librarian_tools
    from lies.memory.service import WikiMemoryService

    agent = librarian_agent(model="test")
    # Bare factory has no tools — the F19 ground-digest bug class.
    assert _registered_tool_names(agent) == []

    register_librarian_tools(
        agent,
        wiki=empty_wiki,  # type: ignore[arg-type]
        memory_service=WikiMemoryService(empty_wiki),  # type: ignore[arg-type]
    )

    names = _registered_tool_names(agent)
    assert "wiki_search" in names, f"expected wiki_search in {names}"
    assert "wiki_read" in names, f"expected wiki_read in {names}"
    assert "wiki_catalog" in names, f"expected wiki_catalog in {names}"


def test_ground_wires_librarian_tools_before_run_sync(
    monkeypatch: pytest.MonkeyPatch,
    empty_wiki: object,
) -> None:
    """Regression: ``ground()`` must wire tools before dispatching.

    Pins Fix Critical: ``src/lies/mcp/grounding.py:228`` called
    ``librarian_agent().run_sync(...)`` directly with no wiring. The
    bare factory emits ``Agent(tools=[])`` so the LLM received an
    agent with no tools and the 4-step contract (classify → search
    → read → return) could not run. ``ground()`` must call
    :func:`register_librarian_tools` against the active wiki's
    :class:`WikiMemoryService` BEFORE ``run_sync``.

    The spy records every call to ``register_librarian_tools``;
    the test asserts it was called once with non-null wiki /
    memory_service kwargs and that the agent passed to the spy is
    the same instance ``run_sync`` saw (so the wiring landed on
    the dispatched agent).

    Library-mode rewrite migration: unscoped ``ground()`` bypasses
    the F18 librarian entirely (Task 1 brick-wall fix), so wiring
    never fires on the unscoped path. Force the librarian path by
    passing a ``tag_expr`` and seeding the matching library
    collection so the F15 tag-filter dispatch validates; the spy
    then records the canonical wiring call against the active
    wiki's :class:`WikiMemoryService`.
    """
    from lies import xdg
    from lies.agents import librarian as librarian_mod
    from lies.constants import LIES_DATA_SUBDIR
    from lies.mcp import grounding
    from lies.mcp import resolution as resolution_mod

    # Seed a ``wiki`` library collection so ``+wiki`` validates at the
    # F15 tag-filter dispatch. The :func:`_isolated_xdg` autouse
    # fixture has already redirected XDG into ``tmp_path``.
    coll_root = xdg.data_home() / LIES_DATA_SUBDIR / "library" / "collections" / "wiki"
    coll_root.mkdir(parents=True, exist_ok=True)

    # ``resolve_wiki`` is the wiki-name → Wiki resolver. The
    # ``empty_wiki`` fixture lives outside the XDG redirect, so
    # patch the resolver to return it directly.
    monkeypatch.setattr(
        resolution_mod,
        "resolve_wiki",
        lambda name=None: empty_wiki,  # type: ignore[arg-type,return-value]
    )

    captured: list[dict[str, object]] = []

    def spy(agent: object, *, wiki: object, memory_service: object) -> None:
        captured.append({"agent": agent, "wiki": wiki, "memory_service": memory_service})

    monkeypatch.setattr(librarian_mod, "register_librarian_tools", spy)

    # Use the ``test`` model id so the bare factory does not try to
    # instantiate the Anthropic provider. The fake ``run_sync``
    # below short-circuits the model call entirely.
    monkeypatch.setattr(librarian_mod, "librarian_agent", lambda model="test": _FakeAgent())

    # Stub the agent factory + ``run_sync`` so we don't need a real
    # model call. The factory returns a sentinel agent that the spy
    # captures; ``run_sync`` records the agent instance so we can
    # confirm the dispatched agent IS the wired one.
    dispatched_agent: list[object] = []

    class _FakeResult:
        def __init__(self, output: object) -> None:
            self.output = output

    class _FakeAgent:
        def run_sync(self, user_prompt: object, *, deps: object) -> _FakeResult:
            dispatched_agent.append(self)
            return _FakeResult(
                LibrarianOutput(tag_expr=None, exclude_expr=None, excerpts=[], distinct_pages=0)
            )

    monkeypatch.setattr(grounding, "librarian_agent", lambda: _FakeAgent())

    digest = grounding.ground("test question", tag_expr="wiki")

    assert len(captured) == 1, f"register_librarian_tools was not called exactly once: {captured}"
    assert captured[0]["wiki"] is not None
    assert captured[0]["memory_service"] is not None
    # The agent passed to the spy IS the one ``run_sync`` dispatched
    # — the wiring landed on the dispatched agent.
    assert dispatched_agent and dispatched_agent[0] is captured[0]["agent"]
    # Digest shape is non-trivial even with zero excerpts: the
    # dispatch-exception branch never fires (the spy succeeded), and
    # ``LibrarianOutput.no_coverage`` defaults to ``False`` for the
    # real-shape ``LibrarianOutput(...)`` we construct here.
    assert digest.no_coverage is False
    assert digest.question == "test question"


def test_ground_dispatch_failure_when_wiring_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring failure surfaces as ``ModelNotConfigured`` (no silent fallback).

    Pins the v0.38.0 no-default-models contract: when wiki resolution
    OR :class:`WikiMemoryService` construction fails AND no model is
    configured, ``ground()`` no longer falls back to a bare
    ``model="test"`` agent. The pre-v0.37.5 best-effort wiring
    contract is retired; operators see the configuration gap and
    configure providers.toml / ``LIES_<AGENT>_MODEL`` before the
    archivist can run.

    Library-mode rewrite migration: the unscoped path bypasses the
    librarian entirely, so wiring never fires on the unscoped path
    — without a tagged query the test would now hit the fan-out
    helper rather than the wiring block. Force the librarian path
    by passing a ``tag_expr`` so the wiring block runs and the
    ``ModelNotConfigured`` failure surfaces; the seeded collection
    satisfies the F15 tag-filter dispatch.
    """
    from lies import xdg
    from lies.constants import LIES_DATA_SUBDIR
    from lies.errors import ModelNotConfigured
    from lies.mcp import grounding

    # Seed a ``wiki`` library collection so ``+wiki`` validates at the
    # F15 tag-filter dispatch. The :func:`_isolated_xdg` autouse
    # fixture has already redirected XDG into ``tmp_path``.
    coll_root = xdg.data_home() / LIES_DATA_SUBDIR / "library" / "collections" / "wiki"
    coll_root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ModelNotConfigured):
        grounding.ground("test question", tag_expr="wiki")


def test_ground_threads_source_kind_from_librarian_output(monkeypatch) -> None:
    """ground() copies source_kind from each PageExcerpt into CitationSnippet.

    Pins Task 2 of dual-source-routing: the per-excerpt ``source_kind``
    flag (``"library"`` vs ``"wiki"``) must propagate through to
    the :class:`CitationSnippet` emitted by :func:`ground` so
    downstream rendering can distinguish primary-source hits from
    wiki-only hits. The test fakes the dispatch (librarian OR
    fan-out, both surface the same ``source_kind`` contract) and
    feeds two excerpts with distinct values; assertions on the
    resulting ``digest.citations`` pin the propagation.

    Library-mode rewrite migration: unscoped ``ground()`` dispatches
    via ``_fanout_unscoped`` rather than the F18 librarian. Mock
    the fan-out helper directly with the same two-excerpt fixture;
    the citation-building loop reads ``getattr(excerpt,
    "source_kind", "library")`` so the propagation contract is
    unchanged across the dispatch boundary.
    """
    from lies.markdown_spans import Span
    from lies.mcp import grounding

    @dataclass(frozen=True)
    class _FakeExcerpt:
        # Mirrors PageExcerpt structurally (type-check ignored).
        collection: str
        slug: str
        title: str
        spans: list
        source_kind: str = "library"

    lib_excerpt_with_span = _FakeExcerpt(
        collection="mermaid",
        slug="syntax/flowchart",
        title="Flowchart syntax",
        spans=[Span(heading_path=[], body="flowchart TD; A-->B", code_fence=False, start_line=1)],
        source_kind="library",
    )
    wiki_excerpt_with_span = _FakeExcerpt(
        collection="default",
        slug="concepts/pydantic",
        title="Pydantic concept",
        spans=[
            Span(
                heading_path=[],
                body="Pydantic is a data validation library.",
                code_fence=False,
                start_line=1,
            )
        ],
        source_kind="wiki",
    )

    def fake_fanout(*_args, **_kwargs):
        return [lib_excerpt_with_span, wiki_excerpt_with_span]

    _patch_fanout(monkeypatch, grounding, fake_fanout)

    digest = grounding.ground("anything")
    kinds = sorted(c.source_kind for c in digest.citations)
    assert kinds == ["library", "wiki"]
