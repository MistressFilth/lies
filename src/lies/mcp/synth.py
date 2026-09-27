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

``ask`` is the new (Task 7) orchestrator that replaces ``synthesize``
in v0.40. It runs the F18 librarian agent (4-step classify→search→
read→return) before the synthesizer; the helpers
:func:`librarian_agent_run`, :func:`synthesizer_agent_run`, and
:func:`_build_query_deps` are wired in Task 9 and currently raise
``NotImplementedError``. Tests patch them directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

from lies.mcp.grounding import ground
from lies.query.citation import Citation

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

    # 2. Empty digest → honest gap prose, no LLM call.
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
        )

    # 3. Re-hydrate ArchivistDigest → LibrarianOutput-compatible PageExcerpts.
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

    # 4. Build the QueryDeps the synthesizer consumes.
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

    # 5. Resolve the synthesizer model.
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

    # 6. Run the synthesizer agent.
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
# ask — Task 7 orchestrator (replaces ``synthesize`` in v0.40)
# ---------------------------------------------------------------------------


def librarian_agent_run(deps: Any) -> Any:
    """Wrapper around the librarian subagent's ``run_sync``.

    Production wiring lands in Task 9; tests patch this directly via
    ``monkeypatch.setattr("lies.mcp.synth.librarian_agent_run", ...)``.
    The shape of ``deps`` (:class:`LibrarianDeps` or successor) is
    settled in Task 8.
    """
    raise NotImplementedError("librarian_agent_run wired in Task 9")


def synthesizer_agent_run(librarian_output: Any, question: str) -> Any:
    """Wrapper around the synthesizer subagent's ``run_sync``.

    Production wiring lands in Task 9; tests patch this directly via
    ``monkeypatch.setattr("lies.mcp.synth.synthesizer_agent_run", ...)``.
    """
    raise NotImplementedError("synthesizer_agent_run wired in Task 9")


# Module-level agent factories — placeholders so tests can
# ``monkeypatch.setattr("lies.mcp.synth.librarian_agent", ...)`` /
# ``"lies.mcp.synth.synthesizer_agent", ...)`` without
# ``AttributeError``. The real factories come from
# :func:`lies.agents.librarian.librarian_agent` and
# :func:`lies.agents.query_synthesizer.query_synthesizer_agent` and
# land in Task 9. Tests don't actually invoke these placeholders —
# the brief's tests patch the ``*_run`` wrappers, not the factories.
librarian_agent: Any = None
synthesizer_agent: Any = None


def _build_query_deps(
    *,
    question: str,
    tag_expr: str | None,
    exclude_tags: list[str] | None,
) -> Any:
    """Build the deps object for :func:`librarian_agent_run`.

    Production wiring lands in Task 9; the stub returns ``None`` so
    tests can monkeypatch :func:`librarian_agent_run` directly without
    patching this helper (the test path goes
    ``_build_query_deps → librarian_agent_run`` and the test patches
    the second hop). The exact deps shape (:class:`LibrarianDeps` plus
    exclude-expr handling, or a successor in Task 8) is settled there.
    """
    return None


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
# ``ask.fn(...)``. Mirrors the pattern in ``search.py`` (Task 4)
# and ``read.py`` (Task 5). Server registration is a separate
# concern (Task 6+); the Tool object is constructed here so downstream
# code can ``import synth`` and call ``ask.fn`` without spinning up an
# MCP instance.
ask = Tool.from_function(_ask_impl, name="ask")
