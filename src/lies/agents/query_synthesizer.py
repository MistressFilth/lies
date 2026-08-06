"""query-synthesizer sub-agent: turn qmd search results into a cited answer."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model

from lies.agents.base import make_sub_agent


class QueryAnswer(BaseModel):
    """A synthesized answer to a user question."""

    answer: str
    """The answer body in markdown."""

    citations: list[str]
    """Wiki-relative paths of pages cited in the answer."""

    should_file: bool
    """True if the answer is worth keeping as a new wiki page."""


QUERY_SYNTHESIZER_SYSTEM_PROMPT = """Your job is to answer the user's question
using only what the LIES wiki contains.

You receive:
- The user's question
- A list of pages (top-N from qmd hybrid search), each with its content

Read each page carefully. Synthesize a markdown answer that:

1. **Cites every claim** with `[page-name](wiki-relative-path)` links.
2. **Quotes the wiki verbatim** when the wording matters. Don't paraphrase
   technical terms, version numbers, or quoted material.
3. **Surfaces disagreements** — if two pages disagree, present both views and
   note the disagreement explicitly.
4. **Says what the wiki does NOT know** — if the corpus is silent on something,
   say so. Don't hallucinate.
5. **Decides whether to file** — set `should_file=True` if the answer is a
   novel synthesis, comparison, or analysis that future readers would value.
   Set `should_file=False` for one-off factual lookups.

Return a `QueryAnswer` with:
- `answer`: the markdown body
- `citations`: the wiki-relative paths you cited
- `should_file`: True/False as above
"""


def query_synthesizer_agent(
    model: Model | str = "anthropic:claude-opus-4-7",
) -> Agent[None, QueryAnswer]:
    """Construct the query-synthesizer sub-agent."""
    return make_sub_agent(
        model=model,
        output_type=QueryAnswer,
        system_prompt=QUERY_SYNTHESIZER_SYSTEM_PROMPT,
    )
