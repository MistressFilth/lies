"""linter sub-agent: health-check the wiki per Karpathy's lint pass.

Walks the wiki looking for: contradictions, stale claims, orphans, missing
pages, missing cross-references, data gaps. Outputs a structured report and
a markdown summary.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel
from pydantic_ai import Agent

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


LINTER_SYSTEM_PROMPT = """Your job is to health-check a LIES wiki. You walk
every page in `wiki/` and look for problems.

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
- `pages`: list of wiki-relative paths involved
- `safe_to_fix`: True if the fix is mechanical and reversible (add a cross-ref,
  create a stub page); False if it requires human judgment (resolve a
  contradiction, evaluate evidence)

Then write a `report_markdown` summary: count by category, list HIGH findings
first, then MEDIUM, then LOW. Include a header `## Lint report — YYYY-MM-DD`
and a footer noting which fixes are safe to apply automatically.

Do not modify the wiki yourself. Return the report; the orchestrator decides
whether to apply fixes.
"""


def linter_agent(model: str = "anthropic:claude-opus-4-7") -> Agent[None, LintReport]:
    """Construct the linter sub-agent."""
    return make_sub_agent(
        model=model,
        output_type=LintReport,
        system_prompt=LINTER_SYSTEM_PROMPT,
    )
