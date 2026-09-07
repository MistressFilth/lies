"""Plan-builder + body formatter for the generic page-author surface (F39).

Mirrors F3's `build_synthesis_plan` + `_format_synthesis_body` shape; the
synthesis-type branch is migrated from F3 in Task 5 (so F3 callers can
collapse their wrapper).
"""

from __future__ import annotations

from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from lies.memory.models import MemoryPlan, WikiPlanInvalid  # noqa: F401 — Task 2+ will reference from this module.


class WriteKnowledgeResult(BaseModel):
    """1:1 Pydantic slice for FastMCP serialization of a page write.

    Mirrors ``SynthesizedMcpAnswer`` (server.py:40): drops fields FastMCP
    cannot serialize; ``receipt`` is a serialized ``MemoryReceipt`` dict.

    Attributes:
        page_path: Wiki-relative path written (or ``None`` when cancelled).
        page_type: One of the six ALLOWED_PAGE_TYPES.
        slug: Slug as it exists on disk (may differ from input if renamed).
        collection: The corpus unit (collection name). Ignored for overview.
        op: Operation kind that produced the page.
        receipt: Serialized ``MemoryReceipt`` (errors-as-values).
    """

    model_config = ConfigDict(frozen=True)

    page_path: str | None
    page_type: str
    slug: str
    collection: str
    op: Literal["create", "update", "none"]
    receipt: dict


def build_author_plan(
    *,
    type: Literal["overview", "entity", "concept", "comparison", "source", "synthesis"],
    collection: str,
    slug: str,
    title: str,
    body: str,
    derived_from: list[str],
    tags: list[str],
    sources: list[str],
    exists: Callable[[str], bool],
    sha_lookup: Callable[[str], str] | None = None,
) -> MemoryPlan:
    """Build a single-op MemoryPlan that writes one page to the wiki.

    Skeleton implementation — full behavior lands in Tasks 2-5.

    Raises:
        NotImplementedError: until Tasks 2-5 land.
    """
    raise NotImplementedError("build_author_plan skeleton; see Task 2")


def _format_author_body(
    *,
    type: Literal["overview", "entity", "concept", "comparison", "source", "synthesis"],
    collection: str,
    title: str,
    body: str,
    derived_from: list[str],
    tags: list[str],
    sources: list[str],
) -> str:
    """Format the markdown body for an authored page (skeleton).

    Full behavior lands in Task 2 (frontmatter assembly).
    """
    raise NotImplementedError("_format_author_body skeleton; see Task 2")
