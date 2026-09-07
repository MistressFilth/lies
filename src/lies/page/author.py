"""Plan-builder + body formatter for the generic page-author surface (F39).

Mirrors F3's `build_synthesis_plan` + `_format_synthesis_body` shape; the
synthesis-type branch is migrated from F3 in Task 5 (so F3 callers can
collapse their wrapper).
"""

from __future__ import annotations

import re
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from lies.memory.models import (
    MemoryPlan,
    PageCreate,
    PageUpdate,
    WikiPlanInvalid,
    _PlanOperation,
)


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


_ALLOWED_TYPES: frozenset[str] = frozenset(
    {"overview", "entity", "concept", "comparison", "source", "synthesis"}
)
_TYPE_PLURAL: dict[str, str] = {
    "entity": "entities",
    "concept": "concepts",
    "comparison": "comparisons",
    "source": "sources",
    "synthesis": "synthesis",
    # overview is a singleton; see Task 3.
}


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

    Returns ``PageCreate`` if the slug does not exist; ``PageUpdate``
    (with ``expected_sha256``) if it does.

    The synthesis branch is added in Task 5; overview lands in Task 3.

    Raises:
        WikiPlanInvalid: ``type`` not in ALLOWED_PAGE_TYPES.
        WikiPlanInvalid: ``body`` is empty after strip.
        WikiPlanInvalid: collision exists but ``sha_lookup`` not provided.
    """
    if type not in _ALLOWED_TYPES:
        raise WikiPlanInvalid(f"page type {type!r} not in ALLOWED_PAGE_TYPES")
    if type == "overview":
        # Implemented in Task 3.
        raise NotImplementedError("overview handling lands in Task 3")
    if type == "synthesis":
        # Implemented in Task 5.
        raise NotImplementedError("synthesis handling lands in Task 5")
    if not body.strip():
        raise WikiPlanInvalid("body is empty")

    rel_path = f"{collection}/{_TYPE_PLURAL[type]}/{slug}.md"
    body_md = _format_author_body(
        type=type,
        collection=collection,
        title=title,
        body=body,
        derived_from=derived_from,
        tags=tags,
        sources=sources,
    )
    evidence = [f"{collection}/{slug}"]

    if exists(rel_path):
        if sha_lookup is None:
            raise WikiPlanInvalid(f"collision on {rel_path} but sha_lookup not provided")
        op: _PlanOperation = PageUpdate(
            path=rel_path,
            expected_sha256=sha_lookup(rel_path),
            content=body_md,
            evidence=evidence,
            tag="author",
        )
    else:
        op = PageCreate(
            path=rel_path,
            content=body_md,
            evidence=evidence,
            tag="author",
        )
    return MemoryPlan(operations=[op], rationale="explicit author write", evidence=evidence)


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
    """Frontmatter + body assembly for an authored page.

    YAML safety: ``title`` and ``collection`` are double-quoted. Empty
    lists render as ``[]``. The body is the operator's content verbatim
    (synthesis branch overrides this in Task 5 with the ``## Evidence``
    codepath).
    """
    title_q = '"' + title.replace("\\", "\\\\").replace('"', '\\"') + '"'
    collection_q = '"' + collection.replace("\\", "\\\\").replace('"', '\\"') + '"'

    parts = ["---"]
    parts.append(f"title: {title_q}")
    parts.append(f"type: {type}")
    parts.append(f"collection: {collection_q}")
    parts.append(_yaml_list("tags", tags))
    parts.append(_yaml_list("sources", sources))
    parts.append(_yaml_list("derived_from", derived_from))
    parts.append("---")
    parts.append("")
    parts.append(body)
    return "\n".join(parts)


def _yaml_list(key: str, items: list[str]) -> str:
    if not items:
        return f"{key}: []"
    if all(re.fullmatch(r"[A-Za-z0-9_\-./]+", x) for x in items):
        return f"{key}: [{', '.join(items)}]"
    lines = [f"{key}:"]
    for x in items:
        lines.append(f"  - {x}")
    return "\n".join(lines)
