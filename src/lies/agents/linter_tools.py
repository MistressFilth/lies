"""Linter sub-agent tool registration (N2).

Three tool closures that mirror ``register_librarian_tools`` (F18)
but drive a wiki-wide enumeration + on-demand read pattern instead
of a relevance-search-first pattern. The linter's job is to walk
the full corpus and find cross-page issues (contradictions, stale
claims, data gaps) — the librarian's search-first pattern does
not fit because the linter doesn't know which pages are relevant
until it has the inventory.

Tools registered:

- ``wiki_list_pages`` — full-corpus inventory + size estimate.
- ``wiki_search`` — relevance-based dive (wraps
  ``WikiMemoryService.search``; same contract as the librarian's
  ``wiki_search``).
- ``wiki_read`` — batched fetch (wraps
  ``WikiMemoryService.read``; same contract as the librarian's
  ``wiki_read``).

Closures bind ``wiki`` + ``memory_service`` so the linter agent's
typed deps surface stays narrow (``:class:`LintDeps` is a marker
type after N2).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from pydantic_ai import Agent, RunContext

from lies.agents.linter import LintDeps

if TYPE_CHECKING:
    from lies.memory.service import WikiMemoryService
    from lies.wiki.wiki import Wiki


# Linter's per-batch budget. Sized to fit one model context under the
# 128K local cap with prompt + system overhead — comfortably ≤10
# small pages or ≤3 dense pages per batch. The linter drives this
# budget via the prompt's stop-when-saturated rule (see
# ``LINTER_SYSTEM_PROMPT``); the orchestrator does not enforce it.
_LINTER_BATCH_LIMIT = 10


def register_linter_tools(
    agent: "Agent[LintDeps, Any]",
    *,
    wiki: "Wiki",
    memory_service: "WikiMemoryService",
) -> None:
    """Register ``wiki_list_pages`` / ``wiki_search`` / ``wiki_read`` on the linter.

    Mirrors :func:`lies.agents.librarian.register_librarian_tools` —
    closures bind ``wiki`` + ``memory_service``; ``LintDeps`` stays a
    marker type. Call once per agent instance after construction.
    Pydantic-ai's ``agent.tool(name=...)`` raises on double-register
    with the same name; callers that own an agent lifecycle
    (e.g. the orchestrator) reuse the existing instance.

    Local imports deferred: the catalog reader sits behind the
    wiki-side import chain (``pydantic_ai``, ``fastmcp``); deferring
    keeps the linter module's cheap import cost. ``RunContext`` and
    the cast helper are at module scope so pydantic-ai's
    ``get_type_hints`` resolves tool function annotations.
    """
    from lies.memory.catalog import list_pages as _list_pages
    from lies.memory.catalog import open_catalog as _open_catalog

    def _wiki_list_pages(ctx: RunContext["LintDeps"]) -> str:
        """List every wiki page with path + size estimate as JSON.

        Walks the wiki corpus via the catalog reader (mirrors
        ``_wiki_catalog`` in the librarian) and emits one row per
        page with the wiki-dir-relative path and a character-based
        size estimate (slugs are short; the estimate seeds the
        linter's read-budget planning).

        The linter calls this once per run before any
        ``wiki_read`` batches, so it knows what to cluster and
        cross-compare. The catalog's ``from_path`` path-walk also
        filters out non-existent files; the linter's tool only sees
        real pages.
        """
        conn = _open_catalog(wiki)
        try:
            pages = _list_pages(conn)
        finally:
            conn.close()
        inventory = [
            {
                "path": f"{page.slug}.md" if not page.slug.endswith(".md") else page.slug,
                "size_estimate_tokens": max(1, len(page.slug) // 4),
                "section": page.section.value,
            }
            for page in pages
        ]
        return json.dumps(inventory, indent=2)

    def _wiki_search(
        ctx: RunContext["LintDeps"],
        question: str,
        limit: int = 5,
    ) -> dict[str, object]:
        """Search the wiki for pages relevant to ``question``.

        Wraps :meth:`WikiMemoryService.search` so the authenticated
        evidence set threads into the linter's run state. Same
        contract as the librarian's ``wiki_search``; the linter
        uses it for relevance-based dive when it surfaces a
        candidate claim and wants to confirm via cross-page
        comparison.
        """
        result = memory_service.search(question, limit=limit)
        return cast(dict[str, object], result.model_dump())

    def _wiki_read(
        ctx: RunContext["LintDeps"],
        page_ids: list[str],
    ) -> dict[str, str]:
        """Read full markdown bodies for the given wiki-dir-relative paths.

        Thin wrapper around :meth:`WikiMemoryService.read`. Paths
        must already be authenticated by ``wiki_list_pages`` or
        ``wiki_search`` in the same run; ``WikiMemoryService.read``
        raises ``WikiPageNotFound`` for unknown paths which the
        tool surfaces as a tool error.

        The linter calls this in batches of ≤10 pages (the
        ``_LINTER_BATCH_LIMIT``); the prompt's stop-when-saturated
        rule gates total batches.
        """
        return memory_service.read(page_ids)

    agent.tool(
        name="wiki_list_pages",
        description=(
            "List every wiki page with its wiki-dir-relative path and a "
            "character-based size estimate. Call this once at the start "
            "of the run to enumerate the corpus before cross-comparison."
        ),
    )(_wiki_list_pages)
    agent.tool(
        name="wiki_search",
        description=(
            "Search this wiki for pages relevant to a candidate claim. "
            "Returns bounded hits with page_id values that can be passed "
            "to wiki_read. Use for relevance-driven dive when a candidate "
            "contradiction / stale claim surfaces."
        ),
    )(_wiki_search)
    agent.tool(
        name="wiki_read",
        description=(
            "Read the full markdown body of wiki pages identified by "
            "wiki-dir-relative path. Accepts only paths returned by "
            "wiki_list_pages or wiki_search in the same run. "
            "Batch size ≤ 10 pages per call to fit one model context."
        ),
    )(_wiki_read)


__all__ = ("register_linter_tools",)
