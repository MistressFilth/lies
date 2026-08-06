"""Single source of truth for LIES agent names.

Every agent the orchestrator instantiates must appear in ``AGENT_ROSTER``.
Config validation iterates this tuple; adding a new agent requires touching
both this list and every user-written ``providers.toml``.
"""

from __future__ import annotations

AGENT_ROSTER: tuple[str, ...] = (
    "orchestrator",
    "source_reader",
    "page_writer",
    "indexer",
    "linter",
    "query_synthesizer",
    "enricher",
    "repair",
)
