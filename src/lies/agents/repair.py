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


def repair_agent(model: Any | None = None) -> Agent[RepairAgentDeps, RepairPlan]:
    """Construct the structured-output repair agent."""
    resolved: Any = model if model is not None else "anthropic:claude-sonnet-4-6"
    return Agent(
        resolved,
        deps_type=RepairAgentDeps,
        output_type=RepairPlan,
        system_prompt=REPAIR_AGENT_SYSTEM_PROMPT,
    )
