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
    from lies.memory.retrieval import _page_id_for

    def _wiki_list_pages(ctx: RunContext["LintDeps"]) -> str:
        """List every wiki page with page_id + path + size + cluster metadata.

        Walks the wiki corpus via the catalog reader (mirrors
        ``_wiki_catalog`` in the librarian) and emits one row per
        page whose on-disk file still exists. The catalog may carry
        stale rows after a ``lies catalog reconcile`` race; the
        on-disk existence check filters them out so the linter never
        reads a path that ``WikiMemoryService.read`` would reject.

        Each row carries:

        - ``page_id``: the SHA-1 hash id that
          :meth:`WikiMemoryService.read` expects. Stable across
          runs — the linter must pass this back to ``wiki_read``,
          not the ``path``.
        - ``path``: the wiki-dir-relative markdown path, for human
          display and prompt-side reasoning.
        - ``size_estimate_tokens``: file bytes divided by 4, so a
          30K-token batch budget is reachable (the linter's
          batched-read step relies on the estimate).
        - ``section``, ``type``, ``source_pkg``: cluster signals from
          the catalog row. ``section`` alone is uninformative (every
          wiki-origin row is ``"wiki"``); ``type`` and
          ``source_pkg`` give the linter a usable partition for its
          cross-page comparison pass.

        The linter calls this once per run before any
        ``wiki_read`` batches, so it knows what to cluster and
        cross-compare.
        """
        conn = _open_catalog(wiki)
        try:
            pages = _list_pages(conn)
        finally:
            conn.close()
        inventory = []
        for page in pages:
            rel = f"{page.slug}.md"
            disk_path = wiki.wiki_dir / rel
            if not disk_path.exists():
                # Catalog-vs-disk drift: catalog row exists but the
                # file does not. The linter must not hand a missing
                # path to ``wiki_read``.
                continue
            try:
                size_bytes = disk_path.stat().st_size
            except OSError:
                continue
            inventory.append(
                {
                    "page_id": _page_id_for(rel),
                    "path": rel,
                    "size_estimate_tokens": max(1, size_bytes // 4),
                    "section": page.section.value,
                    "type": page.type,
                    "source_pkg": page.source_pkg,
                }
            )
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
    ) -> dict[str, object]:
        """Read full markdown bodies for the given wiki page IDs.

        Thin wrapper around :meth:`WikiMemoryService.read` with a
        resilience layer: unknown page IDs (a hallucinated id, an
        id from a stale inventory row that slipped past the
        ``wiki_list_pages`` disk-existence filter, an id whose file
        was deleted between the inventory call and the read call)
        are reported in ``unknown_page_ids`` rather than raised.
        The linter can keep going on a partial batch instead of
        failing the whole lint dispatch.

        Page IDs must come from ``wiki_list_pages`` or
        ``wiki_search`` in the same run. ``WikiMemoryService.read``
        raises on any unknown id; the linter's contract is to be
        tolerant — the agent gets a partial result and can correct
        the next batch.

        The linter calls this in batches of ≤10 pages (the
        ``_LINTER_BATCH_LIMIT``); the prompt's stop-when-saturated
        rule gates total batches.
        """
        from lies.memory.models import WikiPageNotFound

        try:
            bodies = memory_service.read(page_ids)
        except WikiPageNotFound as exc:
            # The exception message is the only signal
            # ``WikiMemoryService.read`` emits on the miss path;
            # parse it for the unknown ids so the agent can
            # correct the next batch.
            import re

            match = re.search(r"unknown page_ids: \[(.*?)\]", str(exc), re.DOTALL)
            if match is None:
                unknown = list(page_ids)
            else:
                unknown = [
                    token.strip().strip("'\"")
                    for token in match.group(1).split(",")
                    if token.strip()
                ]
            return {"bodies": {}, "unknown_page_ids": unknown}
        return {"bodies": bodies, "unknown_page_ids": []}

    agent.tool(
        name="wiki_list_pages",
        description=(
            "List every wiki page as a JSON array. Each row carries "
            "page_id (the SHA-1 id for wiki_read), path "
            "(wiki-dir-relative, display only), size_estimate_tokens, "
            "section, type, and source_pkg. Call this once at the "
            "start of the run to enumerate the corpus before "
            "cross-comparison."
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
            "page_id (the SHA-1 ids returned by wiki_list_pages and "
            "wiki_search). Pass page_ids, NOT paths. Returns "
            "{bodies: {page_id: markdown}, unknown_page_ids: [...]} "
            "— unknown ids are reported (not raised) so a hallucinated "
            "id does not fail the whole lint dispatch. Batch size ≤ 10 "
            "pages per call to fit one model context."
        ),
    )(_wiki_read)


__all__ = ("register_linter_tools",)
