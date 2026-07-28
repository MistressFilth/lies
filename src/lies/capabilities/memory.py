"""Memory harness capability.

Provides cross-session continuity: schema state, last-ingested source,
open lint findings. Per Karpathy: the LLM should "understand what's been
done recently." Memory makes that durable across CLI invocations.
"""
from __future__ import annotations

from typing import Any


def memory(wiki_root: object = None) -> Any:
    """Return a configured Memory capability for the orchestrator.

    The capability is backed by a flat ``"lies"`` namespace. Multi-wiki
    namespacing is not currently supported: harness's ``Memory``
    namespace path-validator rejects absolute paths, so we cannot key
    the namespace on ``wiki_root``. Two wikis opened against the same
    LIES install will share memory state. ``wiki_root`` is accepted for
    forward compatibility but is not used.
    """
    # NOTE: brief shows `from pydantic_ai_harness import Memory`, but
    # `Memory` is exposed under the `memory` subpackage only.
    from pydantic_ai_harness.memory import Memory

    return Memory(namespace="lies")