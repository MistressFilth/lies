"""Synthesize tool: prose answer for human reading.

Calls :func:`lies.mcp.grounding.ground` for retrieval, then runs
:func:`lies.agents.query_synthesizer.query_synthesizer_agent` over
the result. Returns a :class:`SynthesizeEnvelope` carrying the prose
body and claim-tagged citations.

The synthesizer's ``QueryDeps.librarian_output`` is built from the
grounded :class:`ArchivistDigest` by re-hydrating each snippet into
a :class:`PageExcerpt` with a single span holding the snippet body.
This keeps :func:`synthesize` independent of the F18 librarian
LLM round-trip while still feeding the synthesizer the verbatim
excerpts it composes against.

``lib_ask`` is the new (Task 7) orchestrator that replaces ``synthesize``
in v0.40. It runs the F18 librarian agent (4-step classify→search→
read→return) before the synthesizer; the helpers
:func:`librarian_agent_run`, :func:`synthesizer_agent_run`, and
:func:`_build_query_deps` are wired in Task 9 and currently raise
``NotImplementedError``. Tests patch them directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

from lies.mcp.grounding import ground
from lies.query.citation import Citation

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from lies.query.tag_expr import TagExpr


@dataclass(frozen=True)
class SynthesizeEnvelope:
    """The synthesize tool's return value.

    Attributes:
        question: Echoed back for caller verification.
        tag_expr: Chosen union (None when untagged).
        answer: LLM-written prose body in markdown.
        citations: Claim-tagged citations with verbatim quotes.
        pages_read: Slugs the synthesizer used.
        fallback_used: True when retrieval returned no usable hits.
        synthesis_used: True when the LLM ran; False on model error
            (caller should read fallback_reason).
        fallback_reason: Error message when synthesis_used=False.
    """

    question: str
    tag_expr: str | None
    answer: str
    citations: list["Citation"]
    pages_read: list[str]
    fallback_used: bool
    synthesis_used: bool
    fallback_reason: str | None = None
    searched_scope: list[str] = field(default_factory=list)


_FILE_BACK_DEFERRED_MSG = "file_back deferred until v0.41"


def _resolve_synthesizer_model():
    """Resolve the ``query_synthesizer`` model string.

    Mirrors ``Orchestrator._resolve_default_models``: ``env_override``
    first (cheapest), then a user-level ``providers.toml`` load via
    :func:`lies.providers.resolve_model`. Raises
    :class:`lies.errors.ModelNotConfigured` when nothing is wired —
    LIES does not silently fall back to a vendor-default model.
    """
    from lies.errors import ModelNotConfigured
    from lies.providers import env_override, load_providers_config
    from lies.providers.resolver import resolve_model
    from lies.xdg import config_home
    from lies.constants import LIES_DATA_SUBDIR

    override = env_override("query_synthesizer")
    if override is not None:
        return override
    providers_path = config_home() / LIES_DATA_SUBDIR / "providers.toml"
    config = load_providers_config(providers_path)
    if config is not None and "query_synthesizer" in config.agents:
        return resolve_model("query_synthesizer", config)
    raise ModelNotConfigured(
        "synthesize() requires the query_synthesizer model. "
        "Set LIES_AGENT_QUERY_SYNTHESIZER_MODEL or configure providers.toml "
        "via `lies providers init`."
    )


async def synthesize(
    question: str,
    tag_expr: str | None = None,
    exclude_expr: "TagExpr | None" = None,
    file_back: bool = False,
) -> SynthesizeEnvelope:
    """Synthesize a prose answer from library collections.

    Calls :func:`ground` for retrieval, then runs the query synthesizer
    agent over the result. Returns a :class:`SynthesizeEnvelope`.

    Args:
        question: Natural-language question.
        tag_expr: Body of a single include expression (no leading
            sigil). None for library-wide.
        exclude_expr: Compiled NOT AST. None when no ``-`` chain.
        file_back: Reserved for write-tool spec. Raises
            ``ToolError`` until the write-tool lands.

    Returns:
        SynthesizeEnvelope carrying the prose body and citations.

    Raises:
        ToolError: When file_back=True.
    """
    if file_back:
        raise ToolError(_FILE_BACK_DEFERRED_MSG)

    from lies.agents.librarian import LibrarianOutput, PageExcerpt
    from lies.agents.query_synthesizer import QueryDeps, query_synthesizer_agent
    from lies.markdown_spans import Span

    # 1. Retrieval
    digest = await ground(
        question=question,
        tag_expr=tag_expr,
        exclude_expr=exclude_expr,
        top_k=10,  # more context for the synthesizer
    )

    # 2. The retrieval did not finish → inconclusive, not empty. This
    # branch is why ``ArchivistDigest.transient`` exists: a dispatch
    # failure and a clean miss both arrive with zero citations, and
    # collapsing them tells the user their library has nothing when
    # the process is at fault. The field was set at three sites and
    # read at none.
    if digest.transient:
        return SynthesizeEnvelope(
            question=question,
            tag_expr=tag_expr,
            answer=(
                "Retrieval could not complete, so this lookup is "
                "inconclusive — it is not a statement about the library."
            ),
            citations=[],
            pages_read=[],
            fallback_used=True,
            synthesis_used=False,
            fallback_reason="ground() dispatch failed; no corpus claim made",
            searched_scope=list(digest.searched_scope or []),
        )

    # 3. Empty digest → honest gap prose, no LLM call.
    if not digest.citations:
        return SynthesizeEnvelope(
            question=question,
            tag_expr=tag_expr,
            answer="No relevant content found in library.",
            citations=[],
            pages_read=[],
            fallback_used=True,
            synthesis_used=False,
            fallback_reason="ground() returned no citations",
            searched_scope=list(digest.searched_scope or []),
        )

    # 4. Re-hydrate ArchivistDigest → LibrarianOutput-compatible PageExcerpts.
    excerpts: list[PageExcerpt] = []
    for cite in digest.citations:
        span = Span(
            heading_path=[],
            body=cite.snippet,
            code_fence=False,
            start_line=1,
        )
        excerpts.append(
            PageExcerpt(
                collection=cite.collection,
                slug=cite.slug,
                title=cite.title,
                spans=[span],
                source_kind=cite.source_kind,
            )
        )

    # 5. Build the QueryDeps the synthesizer consumes.
    lib_out = LibrarianOutput(
        tag_expr=tag_expr,
        exclude_expr=exclude_expr,
        excerpts=excerpts,
        distinct_pages=len({e.slug for e in excerpts}),
        no_coverage=digest.no_coverage,
    )
    deps = QueryDeps(
        question=question,
        librarian_output=lib_out,
        format_hint="md",
    )

    # 6. Resolve the synthesizer model.
    try:
        model = _resolve_synthesizer_model()
    except Exception as exc:
        return SynthesizeEnvelope(
            question=question,
            tag_expr=tag_expr,
            answer="",
            citations=[],
            pages_read=[e.slug for e in excerpts],
            fallback_used=True,
            synthesis_used=False,
            fallback_reason=f"{type(exc).__name__}: {exc}",
        )

    # 7. Run the synthesizer agent.
    #
    # ``synthesize`` is itself ``async`` (so it can ``await ground()``
    # from inside the daemon's event loop without ``asyncio.run``).
    # pydantic-ai's ``Agent.run_sync`` shells through
    # ``asyncio.run(...)``, which raises ``RuntimeError: This event
    # loop is already running`` when called from inside a running loop
    # — the same shape as the ground bug this commit fixed. Call the
    # async ``agent.run`` directly so the running loop owns the
    # coroutine; the result is the same ``AgentRunResult`` shape, and
    # ``.output`` lands on the same typed ``QueryAnswer``.
    try:
        agent = query_synthesizer_agent(model=model)
        result = await agent.run(question, deps=deps)
        answer_obj = result.output
    except Exception as exc:
        return SynthesizeEnvelope(
            question=question,
            tag_expr=tag_expr,
            answer="",
            citations=[],
            pages_read=[e.slug for e in excerpts],
            fallback_used=True,
            synthesis_used=False,
            fallback_reason=f"{type(exc).__name__}: {exc}",
        )

    return SynthesizeEnvelope(
        question=question,
        tag_expr=tag_expr,
        answer=answer_obj.answer,
        citations=[],  # populated by claim_citations if needed
        pages_read=[e.slug for e in excerpts],
        fallback_used=False,
        synthesis_used=True,
    )


# ---------------------------------------------------------------------------
# lib_ask — Task 7 orchestrator (replaces ``synthesize`` in v0.40)
# ---------------------------------------------------------------------------


def _resolve_librarian_model():
    """Resolve the ``librarian`` model string.

    Mirrors :func:`_resolve_synthesizer_model`: ``env_override``
    first (cheapest), then a user-level ``providers.toml`` load via
    :func:`lies.providers.resolve_model`. Raises
    :class:`lies.errors.ModelNotConfigured` when nothing is wired —
    LIES does not silently fall back to a vendor-default model.
    """
    from lies.errors import ModelNotConfigured
    from lies.providers import env_override, load_providers_config
    from lies.providers.resolver import resolve_model
    from lies.xdg import config_home
    from lies.constants import LIES_DATA_SUBDIR

    override = env_override("librarian")
    if override is not None:
        return override
    providers_path = config_home() / LIES_DATA_SUBDIR / "providers.toml"
    config = load_providers_config(providers_path)
    if config is not None and "librarian" in config.agents:
        return resolve_model("librarian", config)
    # Final fallback: the ``query_synthesizer`` slot. Both agents are
    # part of the same Tier-2 query path and share the same retrieval
    # envelope, so a single configured slot is enough to drive the
    # librarian's dispatch.
    if config is not None and "query_synthesizer" in config.agents:
        return resolve_model("query_synthesizer", config)
    raise ModelNotConfigured(
        "lib_ask() requires the librarian model. "
        "Set LIES_AGENT_LIBRARIAN_MODEL or configure providers.toml "
        "via `lies providers init`."
    )


def librarian_agent_run(deps: Any) -> Any:
    """Run the librarian subagent's ``run_sync`` and return the LibrarianOutput.

    Builds a fresh librarian agent (4-step pipeline), wires the new
    stateless MCP-backed tools (``collections_read`` / ``search`` /
    ``read``), and invokes ``run_sync`` with the supplied deps.

    Tests patch this function directly via
    ``monkeypatch.setattr("lies.mcp.synth.librarian_agent_run", ...)``
    to stub the dispatch. The production path resolves the librarian
    model eagerly via :func:`_resolve_librarian_model`, constructs
    the agent with the v0.40 system prompt, registers the new tool
    set, and propagates the agent's typed output back to the caller
    (pydantic-ai returns a :class:`LibrarianOutput` dataclass
    directly when the registered ``output_type`` matches).

    Args:
        deps: :class:`LibrarianDeps` carrying question / tag_expr /
            exclude_expr / top_k.

    Returns:
        :class:`LibrarianOutput` — the librarian's curated excerpt
        bundle with ``searched_scope`` populated from the search
        tool's resolution.
    """
    from lies.agents.librarian import LibrarianOutput, register_librarian_tools

    # ``librarian_agent`` is imported at module scope below so tests can
    # ``monkeypatch.setattr("lies.mcp.synth.librarian_agent", ...)``.
    agent_factory = librarian_agent

    model = _resolve_librarian_model()
    agent = agent_factory(model=model)
    register_librarian_tools(agent)
    try:
        result = agent.run_sync(deps.question, deps=deps)
    except Exception as exc:
        # Fail-soft: pydantic-ai raises (e.g.
        # ``UsageLimitExceeded("Exceeded maximum output retries (1)")``)
        # when the librarian LLM cannot produce a valid
        # ``LibrarianOutput`` after the configured retry budget. The
        # MCP ``lib_ask`` tool must surface an honest gap envelope instead
        # of crashing the user's request. Match the F18 grounding
        # archivist's contract: return a ``LibrarianOutput`` with
        # empty excerpts and ``no_coverage=True`` so ``_ask_impl``
        # short-circuits to the "No relevant content found" path.
        # ``tag_expr`` / ``exclude_expr`` mirror the request's filters
        # for observability — the synthesizer does not consume them,
        # but a log reader can correlate the fallback against the
        # user's question. ``searched_scope`` is empty because the
        # librarian never executed its ``search()`` tool.
        #
        # ``transient=True, no_coverage=False`` rather than
        # ``no_coverage=True``: the librarian LLM never ran, so this
        # bundle is a fact about the run and carries no information
        # about the corpus. ``no_coverage=True`` here rendered as
        # "No relevant content found in library" on the primary
        # human-facing tool, naming a corpus problem for a model
        # outage. This branch predates the seam work and sat
        # unchanged beside the contract that was rewritten next to
        # it.
        log.warning(
            "librarian_agent_run: dispatch failed (%s: %s); returning transient fallback",
            type(exc).__name__,
            exc,
        )
        return LibrarianOutput(
            tag_expr=getattr(deps, "tag_expr", None),
            exclude_expr=getattr(deps, "exclude_expr", None),
            excerpts=[],
            distinct_pages=0,
            no_coverage=False,
            searched_scope=[],
            transient=True,
        )
    out = result.output
    # Defensive: pydantic-ai's ``output_type=LibrarianOutput`` means
    # ``out`` IS a ``LibrarianOutput`` (the registered dataclass).
    # The ``isinstance`` check is a forward-compat guard against a
    # future migration that emits a pydantic model alongside the
    # dataclass — the conversion below keeps the public surface
    # dataclass-shaped so consumers (synth, MCP layer, tests) see the
    # same shape regardless of the agent's output type.
    if isinstance(out, LibrarianOutput):
        return out
    return LibrarianOutput(
        tag_expr=getattr(out, "tag_expr", None),
        exclude_expr=getattr(out, "exclude_expr", None),
        excerpts=list(getattr(out, "excerpts", []) or []),
        distinct_pages=getattr(out, "distinct_pages", 0),
        no_coverage=getattr(out, "no_coverage", False),
        searched_scope=list(getattr(out, "searched_scope", None) or []),
    )


def synthesizer_agent_run(librarian_output: Any, question: str) -> Any:
    """Run the synthesizer subagent's ``run_sync`` and return its answer.

    Builds a fresh synthesizer agent, wires the librarian's excerpt
    bundle as :class:`QueryDeps`, and returns the typed
    :class:`QueryAnswer`.

    Tests patch this function directly via
    ``monkeypatch.setattr("lies.mcp.synth.synthesizer_agent_run", ...)``
    to stub the dispatch.

    Args:
        librarian_output: :class:`LibrarianOutput` from
            :func:`librarian_agent_run`.
        question: The original user question (echoed into
            :class:`QueryDeps`).

    Returns:
        :class:`QueryAnswer` — the synthesizer's prose answer with
        citations and ``format_hint``.
    """
    from lies.agents.query_synthesizer import QueryDeps, query_synthesizer_agent

    model = _resolve_synthesizer_model()
    agent = query_synthesizer_agent(model=model)
    deps = QueryDeps(
        question=question,
        librarian_output=librarian_output,
        format_hint="md",
    )
    result = agent.run_sync(question, deps=deps)
    return result.output


# Module-level agent factories — the real factories. Tests can
# monkeypatch them via ``monkeypatch.setattr("lies.mcp.synth.librarian_agent", ...)``
# if they need a stubbed factory.
from lies.agents.librarian import librarian_agent  # noqa: E402,F401
from lies.agents.query_synthesizer import query_synthesizer_agent as synthesizer_agent  # noqa: E402,F401


def _build_query_deps(
    *,
    question: str,
    tag_expr: str | None,
    exclude_tags: list[str] | None,
) -> Any:
    """Build the deps object for :func:`librarian_agent_run`.

    Translates the MCP ``lib_ask`` tool's flat ``exclude_tags`` surface
    (a list of bare-tag strings like ``["c:opencode"]``) into the
    compiled ``TagExpr`` AST the librarian consumes in
    :attr:`LibrarianDeps.exclude_expr`. ``None`` when no ``-`` chain
    was supplied; ``list[str]`` chain paths land here as a single
    ``Or(...)`` AST so the F15 grammar walks the full chain site-side.

    Args:
        question: The user's natural-language question.
        tag_expr: Body of a single include expression. ``None`` for
            library-wide.
        exclude_tags: NOT tags without leading sigil. ``None`` when
            no ``-`` chain.

    Returns:
        :class:`LibrarianDeps` carrying the question, tag_expr
        (string body), compiled exclude AST, and ``top_k=5``.
    """
    from lies.agents.librarian import LibrarianDeps
    from lies.query.tag_expr import Include, Or

    exclude_expr: Include | Or | None = None
    if exclude_tags:
        atoms = [
            Include(tag.split(":", 1)[-1], "c" if tag.startswith("c:") else None)
            for tag in exclude_tags
        ]
        if len(atoms) == 1:
            exclude_expr = atoms[0]
        elif len(atoms) > 1:
            # Fold the chain into a left-leaning ``Or`` AST. The F15
            # grammar's walker handles nested Ors uniformly; left-
            # leaning is the historical convention for OR-of-NOT
            # chains.
            exclude_expr = atoms[0]
            for atom in atoms[1:]:
                exclude_expr = Or(exclude_expr, atom)

    return LibrarianDeps(
        question=question,
        tag_expr=tag_expr,
        exclude_expr=exclude_expr,
        top_k=5,
    )


def _ask_impl(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
    file_back: bool = False,
) -> SynthesizeEnvelope:
    """Orchestrate librarian → synthesizer → envelope.

    Replaces the old ``synthesize`` tool. The librarian LLM runs the
    Classify → Search → Read → Return 4-step pipeline; the synthesizer
    LLM produces the prose answer from the librarian's excerpt bundle.

    Args:
        question: Natural-language question.
        tag_expr: Body of a single include expression (no leading
            sigil). ``None`` for library-wide.
        exclude_tags: NOT tags without leading sigil. ``None`` when no
            ``-`` chain.
        file_back: Reserved for the write-tool spec. Raises
            ``ToolError`` until the write-tool lands in v0.41.

    Returns:
        :class:`SynthesizeEnvelope` carrying the prose body and
        citations.

    Raises:
        ToolError: When ``file_back=True``.
    """
    if file_back:
        raise ToolError(_FILE_BACK_DEFERRED_MSG)

    deps = _build_query_deps(question=question, tag_expr=tag_expr, exclude_tags=exclude_tags)
    lib_out = librarian_agent_run(deps)

    if lib_out.transient:
        # The librarian never dispatched. "No relevant content found
        # in library" would name a corpus problem for a model
        # outage, and the string is the one the librarian contract
        # designates as the false claim.
        return SynthesizeEnvelope(
            question=question,
            tag_expr=tag_expr,
            answer=(
                "The librarian could not be dispatched, so this lookup is "
                "inconclusive — it is not a statement about the library."
            ),
            citations=[],
            pages_read=[],
            fallback_used=True,
            synthesis_used=False,
            fallback_reason="librarian dispatch failed; no corpus claim made",
            searched_scope=list(lib_out.searched_scope or []),
        )

    if not lib_out.excerpts:
        return SynthesizeEnvelope(
            question=question,
            tag_expr=tag_expr,
            answer="No relevant content found in library.",
            citations=[],
            pages_read=[],
            fallback_used=True,
            synthesis_used=False,
            fallback_reason="librarian returned no excerpts",
            searched_scope=list(lib_out.searched_scope or []),
        )

    synth_out = synthesizer_agent_run(lib_out, question)

    return SynthesizeEnvelope(
        question=question,
        tag_expr=tag_expr,
        answer=getattr(synth_out, "answer", ""),
        citations=getattr(lib_out, "citations", []) or [],
        pages_read=getattr(synth_out, "pages_read", []) or [],
        fallback_used=bool(getattr(synth_out, "fallback_used", False)),
        synthesis_used=bool(getattr(synth_out, "synthesis_used", True)),
        fallback_reason=getattr(synth_out, "fallback_reason", None),
        searched_scope=list(lib_out.searched_scope or []),
    )


# Wrap as a FastMCP ``Tool`` so the MCP wire can serialize the
# dispatch surface and tests can reach the underlying function via
# ``lib_ask.fn(...)``. Mirrors the pattern in ``search.py`` (Task 4)
# and ``read.py`` (Task 5). Server registration is a separate
# concern (Task 6+); the Tool object is constructed here so downstream
# code can ``import synth`` and call ``lib_ask.fn`` without spinning up an
# MCP instance.
lib_ask = Tool.from_function(_ask_impl, name="lib_ask")
