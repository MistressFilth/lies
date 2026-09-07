"""Generic page-author surface (F39).

Public API:
    build_author_plan: Build a single-op MemoryPlan that writes one page.
    _format_author_body: Frontmatter + body assembly (private; tests import via lies.page).
    WriteKnowledgeResult: Pydantic slice for MCP serialization.

All writes route through WikiMemoryService.apply_plan (F2 envelope). No
code path in this package bypasses it.
"""

from lies.page.author import (
    WriteKnowledgeResult,
    build_author_plan,
    _format_author_body,
)

__all__ = [
    "WriteKnowledgeResult",
    "build_author_plan",
    "_format_author_body",
]
