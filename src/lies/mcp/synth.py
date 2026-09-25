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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from lies.mcp.grounding import ground

if TYPE_CHECKING:
    from lies.query.citation import Citation
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


_FILE_BACK_DEFERRED_MSG = (
    "file_back is deferred in this release; see "
    "superpowers/specs/2026-09-24-library-mode-read-side-rewrite-design.md"
)


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
    digest = ground(
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
    try:
        agent = query_synthesizer_agent(model=model)
        result = agent.run_sync(question, deps=deps)
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
