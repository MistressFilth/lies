"""MemoryEnricher sub-agent.

Produces a :class:`MemoryPlan` from a bounded evidence envelope. The
enricher never mutates the filesystem directly; the host validates
and applies the plan through :class:`WikiMemoryService`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent

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


def enricher_agent(model: Any | None = None) -> Agent[MemoryEnricherDeps, MemoryPlan]:
    """Construct the structured-output MemoryEnricher agent."""
    resolved: Any = model if model is not None else "anthropic:claude-opus-4-7"
    return Agent(
        resolved,
        deps_type=MemoryEnricherDeps,
        output_type=MemoryPlan,
        system_prompt=ENRICHER_SYSTEM_PROMPT,
    )
