"""linter sub-agent: health-check the wiki per Karpathy's lint pass.

Walks the wiki looking for: contradictions, stale claims, orphans, missing
pages, missing cross-references, data gaps. Outputs a structured report and
a markdown summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.tools import RunContext

from lies.agents.base import make_sub_agent


class LintSeverity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LintFinding(BaseModel):
    """A single lint finding."""

    severity: LintSeverity
    category: str  # contradiction | stale | orphan | missing_page | missing_xref | data_gap
    message: str
    pages: list[str]  # wiki-relative paths
    safe_to_fix: bool = False


class LintReport(BaseModel):
    """The result of a lint pass."""

    findings: list[LintFinding]
    report_markdown: str


@dataclass
class LintDeps:
    """Dependencies the linter sub-agent needs to actually look at the wiki.

    ``page_texts`` is a wiki-dir-relative path → markdown body map. The
    orchestrator collects this in ``_call_linter`` so the LLM can read
    every page without needing tool calls. ``wiki_root`` is the absolute
    path to the repo root (where ``.git`` lives) for reference in
    findings' ``pages`` field — the LLM is told to emit paths in the
    same wiki-dir-relative convention as the page text keys so they
    dedup cleanly against the deterministic shell's findings.
    """

    page_texts: dict[str, str]
    wiki_root: str


LINTER_SYSTEM_PROMPT = """Your job is to health-check a LIES wiki.

**Inputs:** the markdown body of every wiki page is provided in your
dependencies (keyed by wiki-dir-relative path, e.g. ``concepts/alpha.md``).
You have NO tool calls — read the page text from the dependencies
directly. Do not try to call tools or read the filesystem.

**Categories** (use these exact strings):

1. **contradiction** — two pages assert conflicting claims about the same thing.
   Read carefully. Surface both pages.
2. **stale** — a page cites a source where newer sources have superseded the claim.
   Surface the page and the newer source.
3. **orphan** — a page with no inbound links from `index.md` or any other page.
   Surface the page.
4. **missing_page** — an entity or concept mentioned in pages but lacking its
   own page. Surface the referencing page(s) and the missing entity/concept.
5. **missing_xref** — two pages that should link to each other but don't.
   Surface both pages.
6. **data_gap** — a question a web search could answer; the corpus is silent
   on something a wiki reader would want to know. Suggest a search query.

For each finding, return a `LintFinding` with:
- `severity`: HIGH (contradictions, data gaps that block understanding),
  MEDIUM (stale, missing_xref), LOW (orphans, missing_page for minor things)
- `category`: one of the strings above
- `message`: a one-sentence description
- `pages`: list of wiki-dir-relative paths (e.g. ``concepts/alpha.md``,
  NO ``wiki/`` prefix) so the deterministic shell's findings dedup
  cleanly against yours
- `safe_to_fix`: True if the fix is mechanical and reversible (add a cross-ref,
  create a stub page); False if it requires human judgment (resolve a
  contradiction, evaluate evidence)

Then write a `report_markdown` summary: count by category, list HIGH findings
first, then MEDIUM, then LOW. Include a header `## Lint report — YYYY-MM-DD`
and a footer noting which fixes are safe to apply automatically.

Do not modify the wiki yourself. Return the report; the orchestrator decides
whether to apply fixes.
"""


def _build_linter_prompt(ctx: RunContext[LintDeps]) -> str:
    """Render the page-text corpus into the linter's system prompt.

    Pydantic-ai deps are ``RunContext`` data — they are NOT
    auto-serialized into model messages. A static ``system_prompt``
    alone leaves the LLM unable to read any wiki page, so the model
    can only hallucinate or return an empty report. This callable
    extends ``LINTER_SYSTEM_PROMPT`` with the actual page corpus so
    the model can read every wiki page directly.

    Page paths are emitted with a ``--- <path> ---`` separator so the
    LLM can attribute claims to specific pages, and so unit tests can
    assert the corpus reached the prompt.
    """
    parts: list[str] = [LINTER_SYSTEM_PROMPT]
    if ctx.deps.wiki_root:
        parts.append(f"\nWiki root: {ctx.deps.wiki_root}")
    for path, text in ctx.deps.page_texts.items():
        parts.append(f"\n--- {path} ---\n{text}")
    return "\n".join(parts)


def linter_agent(model: str = "anthropic:claude-opus-4-7") -> Agent[LintDeps, LintReport]:
    """Construct the linter sub-agent.

    Carries ``LintDeps`` so the orchestrator can pre-supply every wiki
    page's markdown body. ``_build_linter_prompt`` is registered as a
    ``system_prompt`` callable so the page corpus is rendered into the
    prompt at run time — a static ``system_prompt`` alone would leave
    the model unable to read any page.
    """
    agent: Agent[LintDeps, LintReport] = make_sub_agent(
        model=model,
        output_type=LintReport,
        deps_type=LintDeps,
        system_prompt=LINTER_SYSTEM_PROMPT,
    )
    agent.system_prompt(_build_linter_prompt)
    return agent
