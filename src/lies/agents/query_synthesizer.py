"""query-synthesizer sub-agent: turn qmd search results into a cited answer."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.tools import RunContext

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
- **`answer`**: the markdown body
- **`citations`**: data-root-relative POSIX paths matching the keys
  shown in the corpus below (`--- wiki/concepts/alpha.md ---` etc.).
  **Keep the `wiki/` prefix.** The `[name](path)` link inside the
  answer body must use the **same path verbatim** — the orchestrator
  drops citations whose path does not match a retrieved page key, and
  the resulting answer body's link will then point to a non-existent
  page.
- **`should_file`**: True/False as above
"""


@dataclass
class QueryDeps:
    """Dependencies the query-synthesizer needs to answer without tool calls.

    ``page_texts`` is a ``data_root``-relative POSIX path → full
    markdown body map, collected by
    ``Orchestrator._call_query_synthesizer``. Keys carry the ``wiki/``
    prefix (``wiki/concepts/alpha.md``) because that is the convention
    ``PageRead.rel_path`` and ``SynthesizedAnswer.citations`` already
    use — the agent's returned citations must be comparable against the
    retrieved set without translation. This deliberately differs from
    ``LintDeps.page_texts``, which is wiki-dir-relative.

    Full bodies, not excerpts: the prompt requires the agent to quote
    the wiki verbatim and to present both sides when two pages disagree,
    and neither is possible from a truncated excerpt.
    """

    question: str
    page_texts: dict[str, str]


def _build_query_prompt(ctx: RunContext[QueryDeps]) -> str:
    """Render the question and page corpus into the system prompt.

    Pydantic-ai deps are ``RunContext`` data and are NOT auto-serialized
    into model messages, so a static ``system_prompt`` alone would leave
    the agent unable to read any page. Mirrors
    ``lies.agents.linter._build_linter_prompt``.

    Defensive against ``ctx.deps is None`` for callers that drive the
    agent without deps.
    """
    parts: list[str] = [QUERY_SYNTHESIZER_SYSTEM_PROMPT]
    if ctx.deps is None:
        return parts[0]
    parts.append(f"\nQuestion: {ctx.deps.question}")
    for path, text in ctx.deps.page_texts.items():
        parts.append(f"\n--- {path} ---\n{text}")
    return "\n".join(parts)


def query_synthesizer_agent(
    model: Model | str = "anthropic:claude-opus-4-7",
) -> Agent[QueryDeps, QueryAnswer]:
    """Construct the query-synthesizer sub-agent.

    Carries ``QueryDeps`` so the orchestrator can pre-supply the full
    body of every retrieved page. ``_build_query_prompt`` is registered
    as a ``system_prompt`` callable so that corpus is rendered into the
    prompt at run time.
    """
    agent: Agent[QueryDeps, QueryAnswer] = make_sub_agent(
        model=model,
        output_type=QueryAnswer,
        deps_type=QueryDeps,
        system_prompt=QUERY_SYNTHESIZER_SYSTEM_PROMPT,
    )
    agent.system_prompt(_build_query_prompt)
    return agent
