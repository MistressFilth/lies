"""Memory harness capability, isolated per wiki."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def memory(wiki_root: Path) -> Any:
    """Return Memory scoped to one canonical wiki namespace."""
    from pydantic_ai_harness.memory import Memory

    namespace = f"lies:{wiki_root.resolve()}"
    return Memory(namespace=namespace)
