"""query-synthesizer sub-agent: turn librarian-curated excerpts into a cited answer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.tools import RunContext

from lies.agents.base import make_sub_agent
from lies.agents.librarian import LibrarianOutput
from lies.query.citation import ClaimCitation


@dataclass
class QueryAnswer:
    """A synthesized answer to a user question."""

    answer: str
    """The answer body in markdown."""

    citations: list[str]
    """Wiki-relative paths of pages cited in the answer."""

    should_file: bool
    """True if the answer is worth keeping as a new wiki page."""

    format_hint: Literal["md", "table", "marp"] = "md"
    """The auto-routed format. Default "md" keeps existing callers compiling.

    The synthesizer picks this at composition time based on the answer
    content; the orchestrator's --format override re-synthesizes with a
    stronger prompt when the operator disagrees with the auto-route.
    """

    claim_citations: list[ClaimCitation] = field(default_factory=list)
    """Per-claim bindings: each entry says which citation index supports
    a specific claim substring in the answer body.

    The orchestrator validates each entry's ``citation_index`` against
    the ``citations`` list and each ``claim`` against the answer body,
    dropping entries that fail either check. Survivors are forwarded
    to ``SynthesizedAnswer.claim_citations`` for downstream consumers.
    """

    @property
    def format(self) -> Literal["md", "table", "marp"]:
        """F1 back-compat alias for ``format_hint``.

        The pre-F18 ``SynthesizedAnswer`` surface named the validated
        output format ``format``; the F19 dataclass uses
        ``format_hint`` to make room for the F1 override path's
        distinction between agent-pick and caller-driven format. Down-
        level callers (CLI render, integration tests that predate the
        rename) read ``answer.format``; the F19 MCP wire still reads
        ``format_hint`` directly.
        """
        return self.format_hint

    @property
    def synthesis_used(self) -> bool:
        """F18 compat shim — the F19 path always synthesizes, so True."""
        return True

    @property
    def fallback_used(self) -> bool:
        """F18 compat shim — no extractive fallback in the F19 path."""
        return False


QUERY_SYNTHESIZER_SYSTEM_PROMPT = """Your job is to answer the user's question
using only the LIES wiki excerpts the librarian subagent returned.

You receive:
- The user's question
- A list of excerpts (top-K from the librarian), each with its
  content, heading_path spans, and a `[library]` / `[wiki]` source tag

Read each excerpt carefully. Synthesize a markdown answer that:

1. **Cite every factual claim with inline `[[slug]]: "verbatim"` form.**
   For each factual claim, append a citation of the form:
   `[[page-slug]]: "verbatim text from the excerpt that supports the claim"`.
   Example:
   `[[concepts/pydantic]]: "Nested models are validated recursively."`
   The `verbatim text` MUST appear as a substring of the cited
   excerpt's body. The citation goes immediately after the claim
   (end of sentence or clause, no space before).

2. **Return `claim_citations`** as a list of
   `{claim: "<exact substring from body>", citation_index: <int>,
   quote: "<verbatim excerpt text>"}`.
   - `claim` must appear verbatim in the answer body.
   - `citation_index` is 0-based into your `citations` list.
   - `quote` is the verbatim text from the cited excerpt's body
     that supports the claim.
   The orchestrator validates all three checks and drops entries
   that fail.

3. **Verification step** (mandatory — before returning): for each
   `claim_citations` entry, confirm:
   - `claim` substring appears verbatim in the answer body.
   - `citation_index` is a valid index into `citations`.
   - `quote` substring appears verbatim in the cited excerpt body.
   Drop entries that fail any check. Never emit unverified citations.

4. **Cross-references** (no claim support) use `[[page-slug]]` alone,
   without a quote.

5. **Quotes the wiki verbatim** when the wording matters. Don't
   paraphrase technical terms, version numbers, or quoted material.

6. **Surfaces disagreements** — if two excerpts disagree, cite both
   and note the disagreement explicitly.

7. **Says what the wiki does NOT know** — if the corpus is silent on
   something, say so. Don't hallucinate.

8. **Decides whether to file** — set `should_file=True` if the
   answer is a novel synthesis, comparison, or analysis that future
   readers would value (2+ distinct excerpts + no existing wiki
   page + substantive). Set `should_file=False` for one-off
   factual lookups.

9. **Applies the source rule** — library is the primary source of
   truth. Wiki content is supplementary. When sources contradict,
   agree with library. Preserve `[library]` / `[wiki]` source tags
   in your answer (e.g., as a `[library]` prefix on the
   `[name](path)` link).

10. **Cites every page in the excerpts** — include every excerpt in
    the `citations` list, even if it contributed only supporting
    context. The user should see which excerpts informed the
    answer.

**Do not write a footnote block.** The orchestrator does not
append footnotes — the inline `[[slug]]: "verbatim"` form is the
canonical citation surface.

Pick the format that best fits your answer:
- `md` — prose explanations, single-source summaries, narrative
- `table` — comparisons across 2+ items along 2+ axes
- `marp` — step-by-step procedures, presentations

Shape reference:
- `md`: markdown body with `[[slug]]: "verbatim"` per claim.
- `table`: GFM pipe table; citations in a footer row.
- `marp`: `marp: true` frontmatter + slide breaks on `---`.

Return a `QueryAnswer` with:
- `answer`: the body in the chosen format
- `citations`: paths matching excerpt keys
- `claim_citations`: validated per rule 2 + 3
- `should_file`: True/False per rule 8
- `format_hint`: "md" | "table" | "marp"
"""


@dataclass
class QueryDeps:
    """Dependencies the query-synthesizer needs to answer.

    F19: takes ``librarian_output`` (the librarian's curated
    evidence bundle) instead of pre-loaded ``page_texts`` and
    ``page_sources``. The two derived properties below keep the
    prompt-renderer code path compatible with pre-F19 callers —
    code-fence spans are excluded from ``page_texts`` per the F37
    + F19 spec.
    """

    question: str
    librarian_output: LibrarianOutput

    @property
    def page_texts(self) -> dict[str, str]:
        return {
            e.slug: "\n\n".join(s.body for s in e.spans if not s.code_fence)
            for e in self.librarian_output.excerpts
        }

    @property
    def page_sources(self) -> dict[str, Literal["library", "wiki"]]:
        return {
            e.slug: "library" if e.collection != "wiki" else "wiki"
            for e in self.librarian_output.excerpts
        }


def _build_query_prompt(ctx: RunContext[QueryDeps]) -> str:
    """Render the question and page corpus into the system prompt.

    Pydantic-ai deps are ``RunContext`` data and are NOT auto-serialized
    into model messages, so a static ``system_prompt`` alone would leave
    the agent unable to read any page. Mirrors
    ``lies.agents.linter._build_linter_prompt``.

    Each page is rendered with its source tag inline (``[library]`` /
    ``[wiki]``) before the path so the LLM can apply the
    library-wins-on-conflict rule from the prompt body itself, not
    from a separate header. ``page_sources`` keys mirror
    ``page_texts`` keys by construction (the orchestrator populates
    them in lockstep); the renderer falls back to ``"wiki"`` if a
    missing key is ever encountered so a stale ``page_texts`` entry
    doesn't crash the prompt.

    Defensive against ``ctx.deps is None`` for callers that drive the
    agent without deps.
    """
    parts: list[str] = [QUERY_SYNTHESIZER_SYSTEM_PROMPT]
    if ctx.deps is None:
        return parts[0]
    parts.append(f"\nQuestion: {ctx.deps.question}")
    for path, text in ctx.deps.page_texts.items():
        source = ctx.deps.page_sources.get(path, "wiki")
        parts.append(f"\n--- [{source}] {path} ---\n{text}")
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
