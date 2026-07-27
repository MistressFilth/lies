"""DynamicWorkflow harness capability.

Model writes one Python script that orchestrates sub-agents via
`asyncio.gather`; the entire tree runs in one tool call. This is what
enables the "touch 15 files in one pass" Karpathy describes.
"""
from __future__ import annotations

from typing import Any


def dynamic_workflow(agents: list[Any], max_agent_calls: int = 20) -> Any:
    """Return a configured DynamicWorkflow capability.

    Args:
        agents: Sub-agents available to the orchestration workflow.
        max_agent_calls: Host-side ceiling on sub-agent runs to prevent
            runaway execution. Default 20.
    """
    from pydantic_ai_harness.dynamic_workflow import DynamicWorkflow

    return DynamicWorkflow(agents=agents, max_agent_calls=max_agent_calls)
