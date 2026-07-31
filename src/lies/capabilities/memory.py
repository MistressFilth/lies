"""Memory harness capability, scoped per-wiki via WikiIdentity."""
from __future__ import annotations

from typing import Any

from lies.memory.namespace import WikiIdentity


def memory(wiki_root: object) -> Any:
    """Return a configured Memory capability keyed by the wiki's identity.

    Two wikis opened against the same LIES install now receive
    distinct namespaces. ``wiki_root`` is required; the previous
    shared ``"lies"`` namespace is no longer used.
    """
    from pydantic_ai_harness.memory import Memory

    identity = WikiIdentity.from_root(wiki_root)  # type: ignore[arg-type]
    return Memory(namespace=identity.namespace)