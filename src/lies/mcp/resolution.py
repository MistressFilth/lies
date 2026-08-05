"""Wiki resolution helper.

Resolves the active wiki by name (env or explicit), validates registration,
returns a ``Wiki`` with role-routed paths.
"""

from __future__ import annotations

from lies.config import get_wiki_name
from lies.wiki.wiki import Wiki


def resolve_wiki(name: str | None = None) -> Wiki:
    """Resolve a wiki by name. Defaults to ``LIES_WIKI_NAME`` (or ``"default"``)."""
    return Wiki.require(name or get_wiki_name())
