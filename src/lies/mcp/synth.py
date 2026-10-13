"""Synthesize tool: prose answer for human reading.

Two entry points. :func:`synthesize` calls
:func:`lies.mcp.grounding.ground` for retrieval and then runs
:func:`lies.agents.query_synthesizer.query_synthesizer_agent` over the
result, re-hydrating each snippet into a single-span
:class:`PageExcerpt`. :func:`_ask_impl` (the ``lib_ask`` tool) instead
runs the F18 librarian agent's Classify → Search → Read → Return
pipeline before the synthesizer. Both return a
:class:`SynthesizeEnvelope` carrying the prose body and citations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

from lies.mcp.grounding import ground
from lies.query.citation import Citation, citations_from_paths

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

<<<<<<< HEAD
    # 2. The retrieval did not finish → inconclusive, not empty. A
    # dispatch failure and a clean miss both arrive with zero citations,
    # and collapsing them tells the user their library has nothing when
    # the process is at fault. ``fallback_reason`` carries the digest's
    # own reason; only ``ground()`` knows which failure this was.
=======
    # The retrieval did not finish → inconclusive, not empty. This is why
    # ``ArchivistDigest.transient`` exists: a dispatch failure and a clean miss both arrive
    # with zero citations, and collapsing them tells the user their library has nothing
    # when the process is at fault.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
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
            fallback_reason=(
                digest.fallback_reason or "ground() dispatch failed; no corpus claim made"
            ),
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

<<<<<<< HEAD
    # 7. Run the synthesizer agent.
    #
    # ``Agent.run_sync`` shells through ``asyncio.run``, which raises inside a
    # running loop, so call the async ``agent.run`` directly.
=======
    # Run the synthesizer agent. ``synthesize`` is ``async`` so it can ``await ground()``
    # from inside the daemon's loop; ``Agent.run_sync`` shells through ``asyncio.run``,
    # which raises when a loop is already running. Call the async ``agent.run`` directly.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
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
# lib_ask — librarian → synthesizer → envelope
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
    # part of the same query path and share the retrieval envelope, so a
    # single configured slot is enough to drive the librarian's dispatch.
    if config is not None and "query_synthesizer" in config.agents:
        return resolve_model("query_synthesizer", config)
    raise ModelNotConfigured(
        "lib_ask() requires the librarian model. "
        "Set LIES_AGENT_LIBRARIAN_MODEL or configure providers.toml "
        "via `lies providers init`."
    )


def librarian_agent_run(deps: Any) -> Any:
    """Runs the librarian subagent's ``run_sync`` and returns the LibrarianOutput.

    Builds a fresh librarian agent, wires the MCP-backed tools and
    invokes ``run_sync`` with the supplied deps. The bundle passes
    through :func:`~lies.agents.librarian.with_search_diagnostics`, so
    its ``transient`` / ``fallback_reason`` are reconciled against the
    recorded search verdict rather than the model's copy alone.

    Args:
        deps: :class:`LibrarianDeps` carrying question / tag_expr /
            exclude_expr / top_k.

    Returns:
        :class:`LibrarianOutput`.
    """
    from lies.agents.librarian import (
        LibrarianOutput,
        SearchDiagnostics,
        register_librarian_tools,
        with_search_diagnostics,
    )

    agent_factory = librarian_agent

    diagnostics = SearchDiagnostics()

    model = _resolve_librarian_model()
    agent = agent_factory(model=model)
    register_librarian_tools(agent, search_diagnostics=diagnostics)
    # No ``except QmdLockBusy`` arm here, and there must not be one:
    # ``_search_impl`` catches ``Exception`` and returns an envelope, so
    # the exception cannot cross the agent boundary. Its rendering is
    # ``_search_impl``'s own ``fallback_reason``, which ``diagnostics``
    # carries the rest of the way.
    try:
        result = agent.run_sync(deps.question, deps=deps)
    except Exception as exc:
<<<<<<< HEAD
        # Fail-soft: pydantic-ai raises (e.g.
        # ``UsageLimitExceeded``) when the librarian LLM cannot produce a
        # valid ``LibrarianOutput`` after its retry budget, and the MCP
        # ``lib_ask`` tool must surface an honest envelope rather than
        # crashing the user's request.
        #
        # ``transient=True, no_coverage=False`` rather than
        # ``no_coverage=True``: the librarian LLM never ran, so this
        # bundle is a fact about the run and carries no information
        # about the corpus — ``no_coverage=True`` here renders as "No
        # relevant content found in library", naming a corpus problem for
        # a model outage.
        #
        # The bundle still goes through ``with_search_diagnostics``:
        # ``run_sync`` raising does not un-happen a search the model
        # already ran, and a recorded reason names the remedy.
=======
        # Fail-soft: pydantic-ai raises (e.g. ``UsageLimitExceeded``) when the librarian LLM
        # cannot produce a valid ``LibrarianOutput``. Return an honest gap envelope: empty
        # excerpts, ``transient=True``, ``no_coverage=False``. The LLM never ran, so this is a
        # fact about the run; naming a corpus problem for it would be a lie.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
        log.warning(
            "librarian_agent_run: dispatch failed (%s: %s); returning transient fallback",
            type(exc).__name__,
            exc,
        )
        return with_search_diagnostics(
            LibrarianOutput(
                tag_expr=getattr(deps, "tag_expr", None),
                exclude_expr=getattr(deps, "exclude_expr", None),
                excerpts=[],
                distinct_pages=0,
                no_coverage=False,
                searched_scope=[],
                transient=True,
                fallback_reason=f"librarian dispatch failed: {type(exc).__name__}: {exc}",
            ),
            diagnostics,
        )
    out = result.output
<<<<<<< HEAD
    # Forward-compat guard: a future pydantic model in place of the
    # dataclass still yields a dataclass-shaped bundle.
=======
    # Defensive: ``output_type=LibrarianOutput`` means ``out`` already is one. The
    # ``isinstance`` check is forward-compat against a future pydantic model, and the
    # conversion keeps the public surface dataclass-shaped for consumers.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
    if isinstance(out, LibrarianOutput):
        bundle = out
    else:
        bundle = LibrarianOutput(
            tag_expr=getattr(out, "tag_expr", None),
            exclude_expr=getattr(out, "exclude_expr", None),
            excerpts=list(getattr(out, "excerpts", []) or []),
            distinct_pages=getattr(out, "distinct_pages", 0),
            no_coverage=getattr(out, "no_coverage", False),
            searched_scope=list(getattr(out, "searched_scope", None) or []),
            # Carried for the same reason as the fields above: this branch
            # is a shape conversion, and a field the model set is data.
            transient=bool(getattr(out, "transient", False)),
            fallback_reason=getattr(out, "fallback_reason", None),
        )
    return with_search_diagnostics(bundle, diagnostics)


def synthesizer_agent_run(librarian_output: Any, question: str) -> Any:
    """Run the synthesizer subagent's ``run_sync`` and return its answer.

    Builds a fresh synthesizer agent, wires the librarian's excerpt
    bundle as :class:`QueryDeps`, and returns the typed
    :class:`QueryAnswer`.

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


# Module-level agent factories. Tests can monkeypatch them via
# ``monkeypatch.setattr("lies.mcp.synth.librarian_agent", ...)``.
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
            # Fold the chain into a left-leaning ``Or`` AST; the F15
            # grammar's walker handles nested Ors uniformly.
            exclude_expr = atoms[0]
            for atom in atoms[1:]:
                exclude_expr = Or(exclude_expr, atom)

    return LibrarianDeps(
        question=question,
        tag_expr=tag_expr,
        exclude_expr=exclude_expr,
        top_k=5,
    )


def _join_reasons(*reasons: str | None) -> str | None:
    """Join distinct reasons into one operator-readable string.

    Args:
        *reasons: Candidate reason strings; ``None`` and blank are dropped.

    Returns:
        The de-duplicated reasons joined by ``"; "``, or ``None`` when
        every candidate was empty.
    """
    parts: list[str] = []
    for reason in reasons:
        text = reason.strip() if isinstance(reason, str) else ""
        if text and text not in parts:
            parts.append(text)
    return "; ".join(parts) or None


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
        # The librarian never dispatched, so the fixed sentence must not
        # name a corpus problem for a model outage. Render the bundle's
        # own reason, which ``search`` fills with the holder pid, the
        # elapsed wait, the budget and the contended inode; the fixed
        # wording is only the fallback for a bundle carrying none.
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
            fallback_reason=(
                lib_out.fallback_reason or "librarian dispatch failed; no corpus claim made"
            ),
            searched_scope=list(lib_out.searched_scope or []),
        )

    if not lib_out.excerpts:
        # Read the bundle's own reason here too. ``_search_impl`` writes
        # the specific cause into the envelope and
        # ``with_search_diagnostics`` carries it onto the bundle, so an
        # unknown tag and a registered-but-empty collection are already
        # named; the fixed sentence is only the fallback for a bundle
        # that genuinely carries none.
        return SynthesizeEnvelope(
            question=question,
            tag_expr=tag_expr,
            answer="No relevant content found in library.",
            citations=[],
            pages_read=[],
            fallback_used=True,
            synthesis_used=False,
            fallback_reason=lib_out.fallback_reason or "librarian returned no excerpts",
            searched_scope=list(lib_out.searched_scope or []),
        )

    synth_out = synthesizer_agent_run(lib_out, question)

    # Citations come from the SYNTHESIZER, which emits them as path
    # strings per the F19 prompt. Both this call site and the
    # orchestrator's go through :func:`citations_from_paths` so the two
    # surfaces cannot drift apart.
    citations = citations_from_paths(
        list(getattr(synth_out, "citations", []) or []),
        lib_out.excerpts,
    )

    return SynthesizeEnvelope(
        question=question,
        tag_expr=tag_expr,
        answer=getattr(synth_out, "answer", ""),
        citations=citations,
        pages_read=getattr(synth_out, "pages_read", []) or [],
        fallback_used=bool(getattr(synth_out, "fallback_used", False)),
        synthesis_used=bool(getattr(synth_out, "synthesis_used", True)),
        # Both reasons, never the synthesizer's alone: a partly-dropped
        # bundle leaves ``transient`` False, so this is the only arm
        # where the attestation drop reaches the reader.
        fallback_reason=_join_reasons(
            lib_out.fallback_reason, getattr(synth_out, "fallback_reason", None)
        ),
        searched_scope=list(lib_out.searched_scope or []),
    )


<<<<<<< HEAD
# Wrap as a FastMCP ``Tool`` so the MCP wire can serialize the
# dispatch surface and tests can reach the underlying function via
# ``lib_ask.fn(...)``.
=======
# Wrap as a FastMCP ``Tool`` so the wire can serialize the dispatch surface and tests
# can reach the function via ``lib_ask.fn(...)``, mirroring ``search.py`` and
# ``read.py``. The Tool is constructed here so ``lib_ask.fn`` is callable without an
# MCP instance.
>>>>>>> ff22413 (docs(src): bring every body comment block within the 4-line budget)
lib_ask = Tool.from_function(_ask_impl, name="lib_ask")
