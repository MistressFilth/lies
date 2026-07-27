"""Planning harness capability.

Breaks a complex task (e.g., "ingest this source, which touches 15
pages") into an ordered plan of sub-steps. The orchestrator uses this
to decide what to do, in what order, before invoking DynamicWorkflow.
"""
from __future__ import annotations

from typing import Any


def planning() -> Any:
    """Return a configured Planning capability for the orchestrator."""
    from pydantic_ai_harness.planning import Planning

    return Planning()
