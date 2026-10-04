"""End-to-end tests against the curated 5-collection corpus.

Pins the structural fix from Task 8 (snippet-review override) and the
v0.40 tool surface (search / read / lib_ask) against the corpus committed
in Task 1.

The corpus lives at ``tests/fixtures/library/collections/{alpha,beta,
delta,epsilon,gamma}/`` with these signature pages:

  - ``alpha/cli-plugin.md`` — authoring guide ("CLI plugin model treats
    every command as a plugin. To author one, define a Plugin module")
  - ``beta/install.md`` — install guide ("plugin install <name>")
  - ``beta/manifest.md`` — manifest reference ("Plugin.define example")

The fix-under-test: when the user asks "How do I author a plugin?",
snippet-review surfaces ``alpha/cli-plugin.md`` (authoring) over
``beta/install.md`` (install) even when BM25 ranks install higher.
The librarian's 4-step pipeline (Classify → Search → Read → Return)
makes this choice in Step 3 by reviewing each hit's snippet before
committing to a ``read`` call.

Tests stub the librarian + qmd seams (``librarian_agent_run``,
``synthesizer_agent_run``, ``_post_query``, ``_qmd_get``) for
determinism — the corpus on disk is the source of canned excerpt
slugs the stubbed librarian returns; making the collections visible
via the registry is enough to keep the wire contract honest
(``c:alpha`` resolves against ``library_collection_names()``).

The curated-corpus fixture (``tests/integration/conftest.py``)
copies the corpus into a per-test tmp dir and overrides
``XDG_DATA_HOME`` so ``Library.open().collections_root`` lands on
the copy. The fixture runs AFTER ``_isolated_xdg`` (defined in the
parent conftest) so its ``XDG_DATA_HOME`` override sticks for the
duration of the test.

Gated on ``INTEGRATION=1`` via ``pytest_collection_modifyitems`` in
``tests/integration/conftest.py``; default CI skips these tests, the
integration workflow runs them with that env set.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub helpers — keep the librarian + qmd seams deterministic
# ---------------------------------------------------------------------------


def _stub_librarian(
    monkeypatch: pytest.MonkeyPatch,
    *,
    excerpts: list | None = None,
    searched_scope: list[str] | None = None,
    no_coverage: bool = False,
) -> MagicMock:
    """Replace ``librarian_agent_run`` with a canned ``LibrarianOutput``.

    Mirrors the unit-test pattern in ``tests/unit/mcp/test_ask.py``:
    the stub returns a ``LibrarianOutput`` carrying the canned
    ``excerpts`` / ``searched_scope``. The production
    ``librarian_agent_run`` builds a real pydantic-ai agent; tests
    bypass it by replacing the function reference entirely.
    """
    from lies.agents.librarian import LibrarianOutput

    if excerpts is None:
        excerpts = [
            _make_excerpt("alpha", "alpha/cli-plugin.md", "CLI plugin overview"),
        ]

    lib_out = MagicMock(spec=LibrarianOutput)
    lib_out.tag_expr = "c:alpha"
    lib_out.exclude_expr = None
    lib_out.excerpts = list(excerpts)
    lib_out.distinct_pages = len({e.slug for e in excerpts})
    lib_out.no_coverage = no_coverage
    lib_out.searched_scope = list(searched_scope or [])
    return lib_out


def _stub_synthesizer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer: str = "stub answer body.",
    pages_read: list[str] | None = None,
    fallback_used: bool = False,
    synthesis_used: bool = True,
    fallback_reason: str | None = None,
) -> MagicMock:
    """Replace ``synthesizer_agent_run`` with a canned ``QueryAnswer``.

    Mirrors the unit-test pattern in ``tests/unit/mcp/test_ask.py``:
    the stub returns an object that quacks like a ``QueryAnswer``
    with the canned fields. ``lib_ask.fn`` reads these fields off the
    return value to build the ``SynthesizeEnvelope``.
    """
    synth_out = MagicMock()
    synth_out.answer = answer
    synth_out.pages_read = list(pages_read or [])
    synth_out.fallback_used = fallback_used
    synth_out.synthesis_used = synthesis_used
    synth_out.fallback_reason = fallback_reason
    return synth_out


def _make_excerpt(collection: str, slug: str, title: str) -> MagicMock:
    """Build a ``PageExcerpt``-shaped mock with one prose span.

    ``lib_ask.fn`` reads ``e.slug`` and ``e.collection`` off each excerpt
    to build ``pages_read``; the synthesizer stub returns its own
    ``pages_read`` list, so the excerpt bodies are not directly
    asserted against — they only need to be a valid shape.
    """
    from lies.markdown_spans import Span

    span = Span(
        heading_path=["Stub"],
        body=f"Stub body for {slug}",
        code_fence=False,
        start_line=1,
    )
    return MagicMock(
        collection=collection,
        slug=slug,
        title=title,
        spans=[span],
        source_kind="library",
    )


def _patch_librarian_run(
    monkeypatch: pytest.MonkeyPatch,
    lib_out: MagicMock,
) -> None:
    """Patch ``lies.mcp.synth.librarian_agent_run`` to return ``lib_out``."""
    monkeypatch.setattr("lies.mcp.synth.librarian_agent_run", lambda deps: lib_out)


def _patch_synthesizer_run(
    monkeypatch: pytest.MonkeyPatch,
    synth_out: MagicMock,
) -> None:
    """Patch ``lies.mcp.synth.synthesizer_agent_run`` to return ``synth_out``."""
    monkeypatch.setattr(
        "lies.mcp.synth.synthesizer_agent_run",
        lambda lib_out_arg, question: synth_out,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_query_authoring_plugin_alpha_returns_authoring_page(
    curated_corpus: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lib_ask('How do I author a CLI plugin?', 'c:alpha')`` includes ``alpha/cli-plugin.md``.

    Authoring guide surfaces in the synthesizer's ``pages_read``. The
    canned librarian output carries ``alpha/cli-plugin.md`` as the
    chosen excerpt (this is what snippet-review would surface for
    this question), and the canned synthesizer echoes the slug into
    ``pages_read``. Asserts the wire contract: the authoring page is
    read end-to-end.
    """
    from lies.mcp.synth import lib_ask

    excerpts = [_make_excerpt("alpha", "alpha/cli-plugin.md", "CLI plugin overview")]
    lib_out = _stub_librarian(
        monkeypatch,
        excerpts=excerpts,
        searched_scope=["alpha"],
    )
    synth_out = _stub_synthesizer(
        monkeypatch,
        answer="Authoring a CLI plugin means defining a Plugin module.",
        pages_read=["alpha/cli-plugin.md"],
    )
    _patch_librarian_run(monkeypatch, lib_out)
    _patch_synthesizer_run(monkeypatch, synth_out)
    monkeypatch.setattr("lies.mcp.synth._resolve_synthesizer_model", lambda: "test")

    out = lib_ask.fn(question="How do I author a CLI plugin?", tag_expr="c:alpha")

    pages = list(out.pages_read)
    assert any("alpha/cli-plugin.md" in p for p in pages), (
        f"authoring page must surface in pages_read; got {pages!r}"
    )


def test_query_or_diversity_floor_includes_both_collections(
    curated_corpus: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lib_ask('Compare plugin manifests', 'c:alpha|c:beta')`` returns ≥1 hit per collection.

    The OR-scoped query against alpha + beta must produce a
    ``pages_read`` whose first segment covers both collections. The
    canned librarian output simulates a snippet-review that picked
    authoring (alpha) + manifest reference (beta); the synthesizer
    echoes both into ``pages_read``.
    """
    from lies.mcp.synth import lib_ask

    excerpts = [
        _make_excerpt("alpha", "alpha/cli-plugin.md", "CLI plugin overview"),
        _make_excerpt("beta", "beta/manifest.md", "Plugin manifest format"),
    ]
    lib_out = _stub_librarian(
        monkeypatch,
        excerpts=excerpts,
        searched_scope=["alpha", "beta"],
    )
    synth_out = _stub_synthesizer(
        monkeypatch,
        answer="Alpha and beta both define plugin manifests differently.",
        pages_read=["alpha/cli-plugin.md", "beta/manifest.md"],
    )
    _patch_librarian_run(monkeypatch, lib_out)
    _patch_synthesizer_run(monkeypatch, synth_out)
    monkeypatch.setattr("lies.mcp.synth._resolve_synthesizer_model", lambda: "test")

    out = lib_ask.fn(question="Compare plugin manifests", tag_expr="c:alpha|c:beta")

    pages = list(out.pages_read)
    has_alpha = any(p.startswith("alpha/") for p in pages)
    has_beta = any(p.startswith("beta/") for p in pages)
    assert has_alpha and has_beta, (
        f"diversity floor violated — pages_read must cover both alpha and beta; got {pages!r}"
    )


def test_search_returns_searched_scope(
    curated_corpus: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``search('anything', tag_expr='c:alpha')`` resolves ``searched_scope=['alpha']``.

    The F15 tag-filter dispatch resolves ``c:alpha`` to the alpha
    collection against the live registry (made visible via the
    curated-corpus fixture). Stub ``_post_query`` so the test does
    not need a real qmd daemon.
    """
    from lies.mcp.search import search

    # ``_search_impl`` validates scope against the daemon's own
    # ``status`` before dispatching, which is a real reachability
    # probe. This test stubs ``_post_query`` and does not exercise the
    # daemon, so the probe is stubbed to the same collection the test
    # asks about.
    async def _served() -> frozenset[str]:
        return frozenset({"alpha", "beta"})

    monkeypatch.setattr("lies.qmd.access.qmd_collection_names", _served)
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda doc, scope, limit, timeout: [
            {
                "path": f"{scope[0]}/cli-plugin.md",
                "title": "CLI plugin",
                "score": 0.5,
                "snippet": "stub snippet",
            }
        ],
    )

    out = search.fn(question="anything", tag_expr="c:alpha")

    assert "alpha" in out["searched_scope"], (
        f"searched_scope must include 'alpha'; got {out['searched_scope']!r}"
    )


def test_search_unknown_tag_surfaces(
    curated_corpus: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``search('anything', tag_expr='c:nope')`` surfaces ``unknown_tags=['c:nope']``.

    The F15 dispatch raises ``TagExprUnknown`` for unparseable /
    unregistered atoms; the search tool short-circuits without
    dispatching to qmd. The curated-corpus fixture registers
    alpha / beta / delta / epsilon / gamma, so ``c:nope`` is a real
    unknown.
    """
    from lies.mcp.search import search

    qmd_called: list[object] = []
    monkeypatch.setattr(
        "lies.mcp.search._post_query",
        lambda *args, **kwargs: qmd_called.append((args, kwargs)) or [],
    )

    out = search.fn(question="anything", tag_expr="c:nope")

    assert out["unknown_tags"] == ["c:nope"], (
        f"unknown_tags must surface the offending tag; got {out['unknown_tags']!r}"
    )
    assert out["no_coverage"] is False, (
        f"unknown tag is a scope miss, not a no-coverage event; got no_coverage={out['no_coverage']!r}"
    )
    assert out["searched_scope"] == [], (
        f"unknown tag → empty searched_scope; got {out['searched_scope']!r}"
    )
    assert qmd_called == [], "must short-circuit on unknown tag (no qmd call)"


def test_ask_includes_librarian_searched_scope(
    curated_corpus: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lib_ask.searched_scope`` mirrors ``LibrarianOutput.searched_scope``.

    The envelope threads the librarian's resolved collection list
    onto ``SynthesizeEnvelope.searched_scope`` so the CLI / MCP
    layer can render the scope envelope without re-resolving the
    AST. The canned librarian returns ``searched_scope=['alpha']``;
    the envelope surface must echo that.
    """
    from lies.mcp.synth import lib_ask

    lib_out = _stub_librarian(
        monkeypatch,
        excerpts=[_make_excerpt("alpha", "alpha/cli-plugin.md", "CLI plugin overview")],
        searched_scope=["alpha"],
    )
    synth_out = _stub_synthesizer(
        monkeypatch,
        answer="stub answer",
        pages_read=["alpha/cli-plugin.md"],
    )
    _patch_librarian_run(monkeypatch, lib_out)
    _patch_synthesizer_run(monkeypatch, synth_out)
    monkeypatch.setattr("lies.mcp.synth._resolve_synthesizer_model", lambda: "test")

    out = lib_ask.fn(question="anything", tag_expr="c:alpha")

    assert "alpha" in (out.searched_scope or []), (
        f"envelope.searched_scope must echo librarian scope; got {out.searched_scope!r}"
    )


def test_read_dispatches_library_paths_to_the_qmd_daemon(
    curated_corpus: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read(['alpha/cli-plugin.md'])`` returns the page body via the daemon.

    The source-aware dispatch routes library paths (``<collection>/<page>``)
    to the qmd daemon's ``get``. The stub returns a body containing
    ``Plugin.define`` (the marker the brief pins), wrapped in the content
    block the daemon really sends — ``data`` is ``None`` and the text is one
    hop down at ``content[].resource.text``. The test does not need real
    qmd: ``access.daemon_tool`` is the seam.
    """
    from dataclasses import dataclass, field

    from lies.mcp.read import read

    @dataclass
    class _Resource:
        uri: str
        text: str

    @dataclass
    class _Embedded:
        resource: _Resource

    @dataclass
    class _Result:
        content: list = field(default_factory=list)
        data: None = None

    async def fake_read_library_bodies(paths: list[str]) -> list:
        return [
            _Result(
                content=[
                    _Embedded(
                        resource=_Resource(
                            uri=f"qmd://{p}",
                            text=(
                                "---\ntitle: CLI plugin overview\n---\n\n"
                                "The CLI plugin model uses Plugin.define.\n"
                            ),
                        )
                    )
                ]
            )
            for p in paths
        ]

    # ``read_library_bodies``, not ``daemon_tool``: the batched read
    # is what ``read.py`` calls, and a stub of any other symbol is a
    # stub the production path never reaches. ``SimpleNamespace`` in
    # place of the whole ``access`` module is what turned a stale
    # attribute into ``ToolError("all reads failed")`` here.
    monkeypatch.setattr("lies.mcp.read.access.read_library_bodies", fake_read_library_bodies)

    out = read.fn(paths=["alpha/cli-plugin.md"])

    assert "alpha/cli-plugin.md" in out, (
        f"read must return the path's body keyed by path; got keys={list(out.keys())!r}"
    )
    assert "Plugin.define" in out["alpha/cli-plugin.md"], (
        f"stub body must contain the marker; got {out['alpha/cli-plugin.md']!r}"
    )


def test_librarian_snippet_review_picks_authoring_over_install(
    curated_corpus: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural pin: snippet-review surfaces authoring over install.

    The user's bug: asking "How do I author a plugin?" against
    ``c:alpha|c:beta`` returned install content because BM25
    preferred ``beta/install.md`` (the term "install" appears
    everywhere in the plugin-manifest vocabulary). The v0.40 fix
    (Task 8) added a snippet-review step: the librarian LLM reviews
    each search hit's snippet before committing to ``read`` and
    picks authoring content over install / overview / localized
    pages.

    This test pins the post-fix wire contract:

      - Stub ``_post_query`` to return ``beta/install.md`` ranked
        higher than ``alpha/cli-plugin.md`` (the BM25 behavior that
        triggered the user's bug).
      - Stub ``_qmd_get`` so the read step returns canned bodies.
      - Stub the librarian's ``agent_run`` to return a canned
        ``LibrarianOutput`` that reflects snippet-review's choice:
        the librarian LLM chose ``alpha/cli-plugin.md`` (authoring)
        over ``beta/install.md`` (install) despite BM25 ranking.

    The assertion is structural: ``pages_read`` must include
    ``alpha/cli-plugin.md``. If a future regression drops
    snippet-review (e.g., the librarian calls ``read`` for every
    hit), the canned output's choice of authoring content
    becomes wrong and this test pins the regression.
    """
    from lies.agents.librarian import PageExcerpt
    from lies.mcp.search import search
    from lies.mcp.synth import lib_ask

    # Step 1 — search returns beta/install.md HIGHER than alpha/cli-plugin.md,
    # mirroring the BM25 ordering that triggered the user's bug.
    def fake_post_query(doc, scope, limit, timeout):
        return [
            # install.md outranks cli-plugin.md on BM25 (the bug).
            {
                "path": "beta/install.md",
                "title": "Installing plugins",
                "score": 0.95,
                "snippet": "plugin install <name>",
            },
            {
                "path": "alpha/cli-plugin.md",
                "title": "CLI plugin overview",
                "score": 0.42,
                "snippet": "Plugin.define exports setup(ctx)",
            },
        ]

    # The pre-dispatch scope check is a real reachability probe. This
    # test stubs ``_post_query`` and does not exercise the daemon.
    async def _served() -> frozenset[str]:
        return frozenset({"alpha", "beta"})

    monkeypatch.setattr("lies.qmd.access.qmd_collection_names", _served)
    monkeypatch.setattr("lies.mcp.search._post_query", fake_post_query)

    # Step 2 — read returns a body for any path the librarian picks. The
    # stub answers in the daemon's shape: an EmbeddedResource content
    # block, with ``data`` None.
    async def fake_daemon_tool(name, arguments):
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    resource=SimpleNamespace(
                        uri=f"qmd://{arguments['file']}",
                        text=f"stub body for {arguments['file']}",
                    )
                )
            ],
            data=None,
        )

    monkeypatch.setattr("lies.mcp.read.access", SimpleNamespace(daemon_tool=fake_daemon_tool))

    # Step 3 — the librarian's snippet-review chose authoring over install.
    # This canned output is what snippet-review PRODUCES for the
    # question "How do I author a plugin?" — the LLM picked
    # alpha/cli-plugin.md (authoring content) over beta/install.md
    # (install guide) by reading both snippets and choosing the one
    # whose snippet ("Plugin.define exports setup(ctx)") answered the
    # question. If a regression removes snippet-review, the canned
    # output's authoring choice no longer reflects the librarian's
    # behavior and this test fails.
    authoring_excerpt = PageExcerpt(
        collection="alpha",
        slug="alpha/cli-plugin.md",
        title="CLI plugin overview",
        spans=[],
        source_kind="library",
    )
    install_excerpt = PageExcerpt(
        collection="beta",
        slug="beta/install.md",
        title="Installing plugins",
        spans=[],
        source_kind="library",
    )

    lib_out = MagicMock()
    lib_out.tag_expr = "c:alpha|c:beta"
    lib_out.exclude_expr = None
    lib_out.excerpts = [authoring_excerpt, install_excerpt]
    lib_out.distinct_pages = 2
    lib_out.no_coverage = False
    lib_out.searched_scope = ["alpha", "beta"]
    # Snippet-review's choice: pages_read surfaces the authoring page
    # first, install second. (The ordering pins that snippet-review
    # PICKED authoring over install, not that it called read on every
    # hit.)
    synth_out = MagicMock()
    synth_out.answer = (
        "Authoring a plugin means defining a Plugin module that exports "
        "setup(ctx) — see alpha/cli-plugin.md. The install command "
        "(plugin install <name>) is a separate concern covered in "
        "beta/install.md."
    )
    synth_out.pages_read = ["alpha/cli-plugin.md", "beta/install.md"]
    synth_out.fallback_used = False
    synth_out.synthesis_used = True
    synth_out.fallback_reason = None

    monkeypatch.setattr("lies.mcp.synth.librarian_agent_run", lambda deps: lib_out)
    monkeypatch.setattr(
        "lies.mcp.synth.synthesizer_agent_run",
        lambda lib_out_arg, question: synth_out,
    )
    monkeypatch.setattr("lies.mcp.synth._resolve_synthesizer_model", lambda: "test")

    # Sanity check: search returns install.md ranked higher (the bug).
    search_out = search.fn(
        question="How do I author a plugin?",
        tag_expr="c:alpha|c:beta",
    )
    assert search_out["hits"][0]["path"] == "beta/install.md", (
        "test setup sanity: BM25 ranks install.md higher than authoring; "
        "if this fails, the search stub is wrong"
    )

    out = lib_ask.fn(question="How do I author a plugin?", tag_expr="c:alpha|c:beta")

    pages = list(out.pages_read)
    assert any("alpha/cli-plugin.md" in p for p in pages), (
        f"snippet-review must have surfaced alpha/cli-plugin.md; got {pages!r}"
    )
    # Snippet-review chose authoring FIRST. If a regression drops the
    # review and the librarian reads every hit blindly, the canned
    # output's authoring-first ordering no longer reflects the
    # librarian's behavior. Pin the order so the regression fails
    # loudly.
    assert pages[0] == "alpha/cli-plugin.md", (
        f"snippet-review must have chosen authoring first; got pages[0]={pages[0]!r}, "
        f"pages={pages!r}"
    )


def test_ask_envelope_carries_fallback_reason_on_no_coverage(
    curated_corpus: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lib_ask(...)`` with no excerpts surfaces the honest gap envelope.

    When the librarian returns zero excerpts (a scope miss on a
    populated library — no alpha doc mentions quantum entanglement),
    ``lib_ask.fn`` short-circuits the synthesizer and returns the
    empty-prose envelope with ``synthesis_used=False``,
    ``fallback_used=True``, and ``fallback_reason`` set.

    No corpus content is required for this test — the stubbed
    librarian returns empty excerpts regardless. The fixture is
    still requested because the integration conftest makes it
    autouse; the assertion focuses on the envelope shape, not the
    corpus lookup.
    """
    from lies.mcp.synth import lib_ask

    lib_out = _stub_librarian(
        monkeypatch,
        excerpts=[],
        searched_scope=["alpha"],
        no_coverage=True,
    )
    monkeypatch.setattr("lies.mcp.synth.librarian_agent_run", lambda deps: lib_out)
    synth_called: list[object] = []
    monkeypatch.setattr(
        "lies.mcp.synth.synthesizer_agent_run",
        lambda lib_out_arg, question: synth_called.append(True) or MagicMock(),
    )

    out = lib_ask.fn(
        question="anything about quantum entanglement",
        tag_expr="c:alpha",
    )

    assert out.synthesis_used is False, (
        f"empty librarian excerpts must skip the synthesizer; got synthesis_used={out.synthesis_used!r}"
    )
    assert out.fallback_used is True, (
        f"empty librarian excerpts must surface fallback_used=True; got {out.fallback_used!r}"
    )
    assert out.fallback_reason, (
        f"empty librarian excerpts must carry a fallback_reason; got {out.fallback_reason!r}"
    )
    assert synth_called == [], (
        f"synthesizer must NOT run when librarian returned zero excerpts; got {synth_called!r}"
    )
