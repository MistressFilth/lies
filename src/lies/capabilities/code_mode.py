"""CodeMode harness capability.

Wraps every tool the agent has access to into a single `run_code` tool,
so multi-file operations (e.g., "update 5 wiki pages atomically") become
one model round-trip instead of N sequential calls.
"""

from __future__ import annotations

from typing import Any


def code_mode() -> Any:
    """Return a configured CodeMode capability for the orchestrator.

    See https://pydantic.dev/docs/ai/harness/ for configuration.
    """
    from pydantic_ai_harness import CodeMode

    return CodeMode()
