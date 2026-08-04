"""repair_agent sub-agent: consumes a LintReport and emits a RepairPlan.

The repair agent NEVER emits an op for a `safe_to_fix=False` finding. It
only knows the 4 primitives: CreateStub, AppendLink, UpdateIndex,
AppendEvidence. Output is a single RepairPlan; noop is the expected
output when no findings are safe to fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.tools import RunContext

from lies.agents.linter import LintReport
from lies.agents.repair_models import RepairPlan

REPAIR_AGENT_SYSTEM_PROMPT = """\
You are the LIES repair agent. You receive a `LintReport` plus the
markdown body of every page named in the report, and propose a single
`RepairPlan` that closes the safe-to-fix findings.

HARD RULES:
- NEVER emit an operation for a finding where `safe_to_fix=False`.
  Contradictions, stale claims, and data gaps require human judgment.
  Surface them in the `rationale` field; leave them unfixed.
- Use ONLY these primitives:
  * `CreateStub(path, title)`           — for missing_page findings
  * `AppendLink(target_path, link_text, anchor?, append_to)` — for missing_xref
  * `UpdateIndex(path="wiki/index.md", title)` — for orphan findings
  * `AppendEvidence(path, expected_sha256, content)` — for evidence-backed refinements
- Every operation MUST include `evidence` referencing the LintReport
  finding it closes (use `f"finding_{i}"` where `i` is the finding's
  index in `LintReport.findings`).
- `UpdateIndex.path` MUST be exactly "wiki/index.md".
- If no findings are safe to fix, return a noop plan (empty
  `operations`). This is the expected output for a clean wiki.
- Append_link targets a page that already exists; create_stub creates
  a new page. Verify the page exists before emitting an AppendLink op.
- For each op, set `finding_index` to the index in LintReport.findings
  that the op closes.

The host validates the plan and applies it through `WikiMemoryService`.
You do not touch the filesystem directly.
"""


@dataclass
class RepairAgentDeps:
    lint_report: LintReport
    page_texts: dict[str, str]


def _build_repair_prompt(ctx: RunContext[RepairAgentDeps]) -> str:
    """Render the LintReport + page corpus into the repair agent's prompt.

    Pydantic-ai deps are ``RunContext`` data, not auto-serialized
    into model messages. A static ``system_prompt`` alone leaves the
    model without access to the lint findings or the page bodies it
    needs to plan against. This callable extends
    ``REPAIR_AGENT_SYSTEM_PROMPT`` with the structured lint report
    and every safe-to-fix finding's page text so the model can emit
    precise edits with the right ``expected_sha256`` and the right
    cross-link targets.

    Defensive against ``ctx.deps is None`` for callers that don't
    pass deps: in that case, return the static prompt alone.
    """
    if ctx.deps is None:
        return REPAIR_AGENT_SYSTEM_PROMPT
    parts: list[str] = [
        REPAIR_AGENT_SYSTEM_PROMPT,
        "\nLint report findings (JSON):\n" + ctx.deps.lint_report.model_dump_json(indent=2),
    ]
    for path, text in ctx.deps.page_texts.items():
        parts.append(f"\n--- {path} ---\n{text}")
    return "\n".join(parts)


def repair_agent(model: Any | None = None) -> Agent[RepairAgentDeps, RepairPlan]:
    """Construct the structured-output repair agent.

    Registers ``_build_repair_prompt`` as a system-prompt callable so
    pydantic-ai renders the lint report and page corpus into the
    prompt at run time. A static ``system_prompt`` alone leaves the
    model unable to see ``RepairAgentDeps``.
    """
    resolved: Any = model if model is not None else "anthropic:claude-sonnet-4-6"
    agent = Agent(
        resolved,
        deps_type=RepairAgentDeps,
        output_type=RepairPlan,
        system_prompt=REPAIR_AGENT_SYSTEM_PROMPT,
    )
    agent.system_prompt(_build_repair_prompt)
    return agent
