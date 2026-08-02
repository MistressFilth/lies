"""Pure validators for wiki memory operations.

The validators never touch the filesystem beyond resolving candidate
paths. They raise typed errors so the service and enricher can fail
plans with the same code paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import frontmatter

from lies.memory.models import (
    EvidenceAppend,
    PageCreate,
    PageUpdate,
    WikiEvidenceMissing,
    WikiPlanInvalid,
    _PlanOperation,
)
from lies.wiki.layout import WikiLayout

ALLOWED_PAGE_TYPES: frozenset[str] = frozenset(
    {"overview", "entity", "concept", "comparison", "source"}
)


def validate_page_path(layout: WikiLayout, path: str) -> Path:
    """Resolve ``path`` against ``<layout.wiki_dir>`` and reject escapes.

    Rejects absolute paths, ``..`` traversal, raw source access, and
    any path that resolves outside ``layout.wiki_dir``.
    """
    if not path:
        raise WikiPlanInvalid("page path is empty")
    candidate = Path(path)
    if candidate.is_absolute():
        raise WikiPlanInvalid(f"page path must be relative: {path}")
    if any(part == ".." for part in candidate.parts):
        raise WikiPlanInvalid(f"page path contains '..': {path}")
    resolved = (layout.wiki_dir / candidate).resolve()
    try:
        resolved.relative_to(layout.wiki_dir.resolve())
    except ValueError as exc:
        raise WikiPlanInvalid(f"page path escapes wiki/: {path}") from exc
    if ".." in resolved.parts:
        raise WikiPlanInvalid(f"page path contains '..': {path}")
    return resolved


def validate_page_type(page_type: str) -> None:
    """Reject unknown page types."""
    if page_type not in ALLOWED_PAGE_TYPES:
        raise WikiPlanInvalid(
            f"unknown page type: {page_type!r}; expected one of {sorted(ALLOWED_PAGE_TYPES)}"
        )


def parse_frontmatter(content: str) -> dict[str, Any]:
    """Return parsed YAML frontmatter, or empty dict if none present."""
    post = frontmatter.loads(content)
    return dict(post.metadata or {})


def validate_frontmatter(frontmatter_dict: dict[str, Any], *, page_type: str) -> None:
    """Validate frontmatter shape for the given page type."""
    validate_page_type(page_type)
    if page_type == "overview" and not frontmatter_dict.get("title"):
        raise WikiPlanInvalid("overview frontmatter requires title")
    declared = frontmatter_dict.get("type")
    if declared is None or str(declared) != page_type:
        raise WikiPlanInvalid(f"frontmatter type missing or does not match page_type {page_type!r}")


def validate_operation_evidence(
    op: _PlanOperation, *, known_references: set[str] | None = None
) -> None:
    """Validate evidence against references authenticated during this turn."""
    if not op.evidence:
        raise WikiEvidenceMissing(f"operation on {op.path!r} lacks evidence")
    if known_references is not None:
        unknown = [reference for reference in op.evidence if reference not in known_references]
        if unknown:
            raise WikiEvidenceMissing(
                f"operation on {op.path!r} has unknown evidence references: {unknown}"
            )
    if isinstance(op, PageUpdate) and not op.expected_sha256:
        raise WikiPlanInvalid(f"update on {op.path!r} requires expected_sha256")
    if isinstance(op, EvidenceAppend) and not op.expected_sha256:
        raise WikiPlanInvalid(f"append on {op.path!r} requires expected_sha256")
    if isinstance(op, PageCreate) and not op.content.strip():
        raise WikiPlanInvalid(f"create on {op.path!r} requires non-empty content")
