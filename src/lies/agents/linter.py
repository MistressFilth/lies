"""linter sub-agent: health-check the wiki per Karpathy's lint pass.

Walks the wiki looking for: contradictions, stale claims, orphans, missing
pages, missing cross-references, data gaps. Outputs a structured report and
a markdown summary.

N2 rewire: pages are pulled on demand via three tool closures
(``wiki_list_pages`` / ``wiki_search`` / ``wiki_read``) registered by
:func:`lies.agents.linter_tools.register_linter_tools`. The linter's
typed deps surface stays narrow (``LintDeps`` is a marker type); the
wiki + memory service close over the tool registration site.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic_ai import Agent
from pydantic_ai.models import Model

from lies.agents.base import make_sub_agent


class LintSeverity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class LintFinding:
    """A single lint finding."""

    severity: LintSeverity
    category: str  # contradiction | stale | orphan | missing_page | missing_xref | data_gap
    message: str
    pages: list[str]  # wiki-relative paths
    safe_to_fix: bool = False


@dataclass
class LintReport:
    """The result of a lint pass."""

    findings: list[LintFinding]
    report_markdown: str


@dataclass
class LintDeps:
    """Marker deps type for the linter sub-agent (N2).

    Pre-N2 the deps carried ``page_texts`` and ``wiki_root``. The
    rewire drops both — pages are pulled on demand via the
    ``wiki_list_pages`` / ``wiki_search`` / ``wiki_read`` tools
    registered by :func:`lies.agents.linter_tools.register_linter_tools`,
    and the wiki context closes over the tool registration site
    (same pattern as the librarian's tools). ``LintDeps`` stays as a
    marker type for the agent's typed deps surface; callers that
    construct ``LintDeps`` only for the agent's deps_type signature
    keep compiling.
    """


LINTER_SYSTEM_PROMPT = """Your job is to health-check a LIES wiki.

**Inputs:** you have three tools — `wiki_list_pages`,
`wiki_search`, and `wiki_read`. Read pages on demand; do not
ask the user for input; do not modify the wiki yourself. Return a
`LintReport`; the orchestrator decides whether to apply fixes.

**Dispatch contract** (follow exactly):

1. **Enumerate.** Call `wiki_list_pages()` once. Build the corpus
   map (path → size estimate). Cluster pages by section / tag /
   topic in your reasoning.

2. **Batched read.** For each cluster, call `wiki_read(paths)` with
   ≤ 10 paths per call. Read enough pages to cross-compare claims
   within a topic. Each batch fits one model context; size the
   batch by `size_estimate_tokens` (sum must stay under ~30K tokens).

3. **Search-driven dive.** When a candidate claim surfaces (a
   possible contradiction, stale citation, data gap), call
   `wiki_search(question=<candidate_claim>)` then `wiki_read` the
   top hits to confirm.

4. **Stop when saturated.** When the most recent `wiki_read` batch
   yielded no new findings and the cross-page comparison for the
   current cluster is complete, stop and emit the report. Do not
   re-read pages you've already loaded.

5. **Emit findings.** Return a `LintReport` with one `LintFinding`
   per issue. Categories (use these exact strings):

   - **contradiction** — two pages assert conflicting claims about
     the same thing. Surface both pages.
   - **stale** — a page cites a source where newer sources have
     superseded the claim. Surface the page and the newer source.
   - **orphan** — a page with no inbound links from `index.md` or
     any other page. Surface the page.
   - **missing_page** — an entity or concept mentioned in pages
     but lacking its own page. Surface the referencing page(s)
     and the missing entity/concept.
   - **missing_xref** — two pages that should link to each other
     but don't. Surface both pages.
   - **data_gap** — a question a web search could answer; the
     corpus is silent on something a wiki reader would want to
     know. Suggest a search query.

   For each finding:
   - `severity`: HIGH (contradictions, data gaps that block
     understanding), MEDIUM (stale, missing_xref), LOW (orphans,
     missing_page for minor things).
   - `category`: one of the strings above.
   - `message`: a one-sentence description.
   - `pages`: list of wiki-dir-relative paths (e.g.
     `concepts/alpha.md`, NO `wiki/` prefix) so the deterministic
     shell's findings dedup cleanly against yours.
   - `safe_to_fix`: True if the fix is mechanical and reversible
     (add a cross-ref, create a stub page); False if it requires
     human judgment (resolve a contradiction, evaluate evidence).

6. **Report.** Write a `report_markdown` summary: count by category,
   list HIGH findings first, then MEDIUM, then LOW. Include a
   header `## Lint report — YYYY-MM-DD` and a footer noting which
   fixes are safe to apply automatically.

The orchestrator's deterministic shell is the safety net for
orphan / missing_xref / missing_page — your job is the
contradiction / stale / data_gap surface plus any high-severity
judgments the shell can't make. If a tool call fails, log it in
the report and continue; the shell still runs.
"""


def linter_agent(model: Model | str = "anthropic:claude-opus-4-7") -> Agent[LintDeps, LintReport]:
    """Construct the linter sub-agent (N2).

    Carries the marker :class:`LintDeps` so the typed deps surface
    stays narrow. Tools (`wiki_list_pages` / `wiki_search` /
    `wiki_read`) are registered separately by
    :func:`lies.agents.linter_tools.register_linter_tools` after
    construction. The orchestrator's `_build` does the wiring.
    """
    agent: Agent[LintDeps, LintReport] = make_sub_agent(
        model=model,
        output_type=LintReport,
        deps_type=LintDeps,
        system_prompt=LINTER_SYSTEM_PROMPT,
    )
    return agent
