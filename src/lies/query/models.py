"""Models for the query layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SynthesizedAnswer:
    """The result of synthesizing an answer from wiki pages.

    Attributes:
        answer: The answer body in markdown.
        citations: Wiki-relative paths of pages cited in the answer.
        pages_read: Wiki-relative paths of pages actually read for the
            synthesis (subset of citations that contributed content).
        fallback_used: True if qmd was unavailable, returned no results,
            or failed for some other reason and we fell back to index.md.
        fallback_reason: One of ``""``, ``"qmd_unavailable"``,
            ``"qmd_no_results"``, ``"qmd_failed"`` — why the fallback
            was triggered (empty when qmd served the query).
        page_links: Full markdown link markup for each cited page
            (``[Title](path)``), suitable for direct inclusion in
            markdown output.
        synthesis_used: True when ``query_synthesizer_agent`` produced
            the answer body; False when the extractive fallback did.
            Independent of ``fallback_used``, which reports retrieval.
        synthesis_reason: A note about the synthesis, not solely a
            failure field. Empty when the agent answered cleanly;
            ``"dropped N unretrieved citation(s): <paths>"`` when the
            agent answered but citations were filtered; and
            ``"<ExcType>: <msg>"`` when the agent failed and the body
            is the extractive fallback.
        should_file: Carried from the agent's ``QueryAnswer``. True
            when the agent judged the answer worth keeping as a wiki
            page. Nothing acts on it yet; F3 (the file-back loop)
            consumes it.
    """

    answer: str
    citations: list[str] = field(default_factory=list)
    pages_read: list[str] = field(default_factory=list)
    fallback_used: bool = False
    fallback_reason: str = ""
    changed_pages: list[str] = field(default_factory=list)
    page_links: list[str] = field(default_factory=list)
    synthesis_used: bool = False
    synthesis_reason: str = ""
    should_file: bool = False
