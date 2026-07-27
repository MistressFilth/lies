"""Memory harness capability.

Provides cross-session continuity: schema state, last-ingested source,
open lint findings. Per Karpathy: the LLM should "understand what's been
done recently." Memory makes that durable across CLI invocations.
"""
from __future__ import annotations

from typing import Any


def memory() -> Any:
    """Return a configured Memory capability for the orchestrator.

    The memory is namespaced per-wiki so multiple wikis don't collide.
    """
    # NOTE: brief shows `from pydantic_ai_harness import Memory`, but
    # `Memory` is exposed under the `memory` subpackage only.
    from pydantic_ai_harness.memory import Memory

    return Memory(namespace="lies")