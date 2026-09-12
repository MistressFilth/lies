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
    PageDelete,
    PageUpdate,
    WikiEvidenceMissing,
    WikiPlanInvalid,
)
from lies.wiki.wiki import Wiki

ALLOWED_PAGE_TYPES: frozenset[str] = frozenset(
    {"overview", "entity", "concept", "comparison", "source", "synthesis"}
)


def validate_page_path(wiki: Wiki, path: str) -> Path:
    """Resolve ``path`` against ``<wiki.wiki_dir>`` and reject escapes.

    Rejects absolute paths, ``..`` traversal, raw source access, and
    any path that resolves outside ``wiki.wiki_dir``.
    """
    if not path:
        raise WikiPlanInvalid("page path is empty")
    candidate = Path(path)
    if candidate.is_absolute():
        raise WikiPlanInvalid(f"page path must be relative: {path}")
    if any(part == ".." for part in candidate.parts):
        raise WikiPlanInvalid(f"page path contains '..': {path}")
    resolved = (wiki.wiki_dir / candidate).resolve()
    try:
        resolved.relative_to(wiki.wiki_dir.resolve())
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
    """Validate frontmatter shape for the given page type.

    The agent's explicit ``type:`` declaration wins when valid — it is
    authoritative. The path-derived ``page_type`` is a hint used to fill
    in when the agent omits the declaration. MiniMax-M3 has been
    observed to flatten the per-collection subdir layout (writing pages
    at ``wiki/<collection>/<file>`` without a ``<type_plural>/`` segment),
    so the path-derived page_type defaulted to ``concept`` but the
    frontmatter's ``type: <actual>`` is what the user sees. Trusting the
    frontmatter lets the ingest proceed with the agent's chosen type.
    """
    validate_page_type(page_type)
    if page_type == "overview" and not frontmatter_dict.get("title"):
        raise WikiPlanInvalid("overview frontmatter requires title")
    declared = frontmatter_dict.get("type")
    if declared is None:
        # No declaration — auto-fill the page_type into the frontmatter
        # on disk later (the on-disk frontmatter is parsed but not
        # rewritten here). Validation passes.
        return
    if str(declared) not in ALLOWED_PAGE_TYPES:
        raise WikiPlanInvalid(
            f"frontmatter type {declared!r} is not a valid page type; "
            f"expected one of {sorted(ALLOWED_PAGE_TYPES)}"
        )


def validate_operation_evidence(
    op: PageCreate | PageUpdate | EvidenceAppend | PageDelete,
    *,
    known_references: set[str] | None = None,
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
