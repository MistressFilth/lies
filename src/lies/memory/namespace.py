"""Stable per-wiki Harness Memory namespace derivation.

A wiki gets a deterministic namespace string so the Harness ``Memory``
capability is scoped per wiki instead of sharing state across all
LIES wikis. The namespace must satisfy the harness validator:

  - relative (no leading slash)
  - no path separators inside the namespace body
  - bounded length

We derive the namespace from a short hash of the resolved wiki root
path. Two wikis at the same path always produce the same namespace;
two different paths produce different namespaces.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_NAMESPACE_PREFIX = "lies"
_NAMESPACE_MAX_LEN = 64


def _shorten_path(wiki_root: Path) -> str:
    """Return a short, stable, separator-free token for a wiki root.

    Uses the first 8 bytes of the SHA-256 of the resolved path,
    rendered as 16 hex characters.
    """
    resolved = str(wiki_root.resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return digest


def memory_namespace(wiki_root: Path) -> str:
    """Return the Harness ``Memory`` namespace for a wiki root.

    The namespace is ``lies-<16-hex-digest>``. It is relative, contains
    no path separators, and stays under 64 characters.
    """
    token = _shorten_path(wiki_root)
    namespace = f"{_NAMESPACE_PREFIX}-{token}"
    assert len(namespace) <= _NAMESPACE_MAX_LEN
    assert "/" not in namespace
    assert not namespace.startswith("/")
    return namespace


@dataclass(frozen=True)
class WikiIdentity:
    """Canonical identity for a wiki, used to scope Harness Memory."""

    wiki_root: Path
    namespace: str

    @classmethod
    def from_root(cls, wiki_root: Path) -> WikiIdentity:
        resolved = Path(wiki_root).expanduser().resolve()
        return cls(wiki_root=resolved, namespace=memory_namespace(resolved))
