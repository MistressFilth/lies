"""Linter sub-agent tool registration (N2)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from pydantic_ai import Agent, RunContext

from lies.agents.linter import LintDeps

if TYPE_CHECKING:
    from lies.memory.service import WikiMemoryService
    from lies.wiki.wiki import Wiki


# The linter's per-batch budget. Sized to fit one model context under the 128K local cap
# with prompt overhead — comfortably ≤10 small pages or ≤3 dense pages. The linter drives
# it via the prompt's stop-when-saturated rule (see ``LINTER_SYSTEM_PROMPT``); the
# orchestrator does not enforce it.
_LINTER_BATCH_LIMIT = 10


def register_linter_tools(
    agent: "Agent[LintDeps, Any]",
    *,
    wiki: "Wiki",
    memory_service: "WikiMemoryService",
) -> None:
    """Register ``wiki_list_pages`` / ``wiki_search`` / ``wiki_read`` on the linter."""
    from lies.memory.catalog import list_pages as _list_pages
    from lies.memory.catalog import open_catalog as _open_catalog
    from lies.memory.retrieval import _page_id_for

    def _wiki_list_pages(ctx: RunContext["LintDeps"]) -> str:
        """List every wiki page with page_id + path + size + cluster metadata."""
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
        """Read full markdown bodies for the given wiki page IDs."""
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
