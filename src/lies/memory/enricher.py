"""MemoryEnricher sub-agent.

Produces a :class:`MemoryPlan` from a bounded evidence envelope. The
enricher never mutates the filesystem directly; the host validates
and applies the plan through :class:`WikiMemoryService`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, RunContext

from lies.memory.models import MemoryPlan

ENRICHER_SYSTEM_PROMPT = """\
You are the LIES memory enricher. You receive a brief evidence envelope
about a single user turn and propose a single MemoryPlan.

Rules:
- Propose operations only for durable project knowledge: facts, source
  claims, concepts, contradictions, crosslinks. Never propose to capture
  user preferences, working decisions, or task history.
- Every operation MUST include evidence references.
- Use CREATE only for new durable knowledge that no existing page
  captures.
- Use UPDATE only when the evidence contradicts or extends an existing
  page. Always include the current content's expected_sha256.
- Use EVIDENCE_APPEND for dated annotations on an existing page.
- Never propose DELETE or RENAME.
- Prefer noop when the answer does not contain durable project knowledge.
"""


@dataclass
class MemoryEnricherDeps:
    answer: str
    pages_read: list[str]
    citations: list[str]
    evidence_text: str
    user_request: str = ""
    current_page_metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    active_schema: str = ""


def _evidence_instructions(ctx: RunContext[MemoryEnricherDeps]) -> str:
    """Render the complete bounded evidence envelope for the model."""
    deps = ctx.deps
    envelope = {
        "user_request": deps.user_request,
        "assistant_answer": deps.answer,
        "pages_read": deps.pages_read,
        "citations": deps.citations,
        "evidence_text": deps.evidence_text,
        "current_page_metadata": deps.current_page_metadata,
        "active_wiki_schema": deps.active_schema,
    }
    return "Evidence envelope (JSON):\n" + json.dumps(envelope, indent=2, sort_keys=True)


def enricher_agent(model: Any | None = None) -> Agent[MemoryEnricherDeps, MemoryPlan]:
    """Construct the structured-output MemoryEnricher agent."""
    resolved: Any = model if model is not None else "anthropic:claude-opus-4-7"
    agent = Agent(
        resolved,
        deps_type=MemoryEnricherDeps,
        output_type=MemoryPlan,
        system_prompt=ENRICHER_SYSTEM_PROMPT,
    )
    agent.system_prompt(_evidence_instructions)
    return agent
