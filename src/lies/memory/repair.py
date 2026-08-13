"""Map a RepairPlan onto existing WikiMemoryService operations.

The 4 repair primitives (CreateStub, AppendLink, UpdateIndex,
AppendEvidence) are translated into the existing PageCreate /
PageUpdate / EvidenceAppend memory operations. apply_repair_plan then
flows through the same flock, atomic_commit, and qmd refresh envelope
as apply_plan.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import frontmatter

from lies.agents.repair_models import (
    AppendEvidence,
    AppendLink,
    CreateStub,
    RepairPlan,
    UpdateIndex,
    _RepairOp,
)
from lies.memory.models import (
    EvidenceAppend,
    MemoryPlan,
    PageCreate,
    PageUpdate,
    _PlanOperation,
)
from lies.memory.service import _page_type_from_dir
from lies.wiki.wiki import Wiki


def _stub_body(title: str) -> str:
    today = datetime.now(tz=UTC).date().isoformat()
    return (
        f"---\n"
        f"title: {title}\n"
        f"type: concept\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        f"---\n"
        f"# {title}\n\n"
        f"Stub. Awaiting first content pass.\n"
    )


def _append_link_body(existing: str, link_text: str, target_path: str, anchor: str = "") -> str:
    """Append a markdown link to the end of existing page content."""
    anchor_part = f"#{anchor}" if anchor else ""
    link = f"[{link_text}]({target_path}{anchor_part})"
    return existing.rstrip() + "\n\n" + link + "\n"


def _update_index_body(existing: str, title: str, path: str) -> str:
    """Append a catalog entry to wiki/index.md."""
    return existing.rstrip() + f"\n- [{title}]({path})\n"


def _ensure_frontmatter_type(content: str, page_type: str) -> str:
    """Ensure ``content`` has a frontmatter block with ``type: <page_type>``.

    Parses the existing frontmatter via ``python-frontmatter`` so list
    values, multi-line values, nested fields, and values that contain
    colons are preserved. Sets the ``type`` field to ``page_type``
    (overriding any existing value) and re-serializes.
    """
    post = frontmatter.loads(content)
    # Mutating post.metadata keeps the post's handler; constructing a new
    # Post(**metadata) is fragile against the library's BaseHandler
    # second-positional argument under static type checkers.
    metadata = post.metadata
    metadata.pop("type", None)
    metadata["type"] = page_type
    dumped = frontmatter.dumps(post)
    # Ensure the file ends with a newline so subsequent appends behave.
    if not dumped.endswith("\n"):
        dumped += "\n"
    return dumped


def _merge_append_links(wiki: Wiki, append_to: str, links: list[AppendLink]) -> PageUpdate:
    """Combine multiple AppendLinks targeting ``append_to`` into one PageUpdate.

    The repair plan allows several AppendLinks on the same target page;
    they each append a markdown link to the same destination. The
    underlying memory service applies one PageUpdate per path, so we
    concatenate the link additions into a single content rewrite and
    emit one operation with the original page's content hash as its
    expected_sha256.
    """
    existing = (wiki.wiki_dir / append_to).read_text(encoding="utf-8")
    page_type = _page_type_from_dir(Path(append_to).parent.name)
    content = existing
    for link in links:
        content = _append_link_body(content, link.link_text, link.target_path, link.anchor)
    content = _ensure_frontmatter_type(content, page_type)
    evidence = [e for link in links for e in link.evidence]
    return PageUpdate(
        path=append_to,
        expected_sha256=_hash_text(existing),
        content=content,
        evidence=evidence,
    )


def _map_non_append_op(op: _RepairOp, wiki: Wiki | None) -> _PlanOperation:
    """Translate a non-AppendLink repair op into its MemoryPlan equivalent."""
    if isinstance(op, CreateStub):
        return PageCreate(
            path=op.path,
            content=_stub_body(op.title),
            evidence=op.evidence,
        )
    if isinstance(op, UpdateIndex):
        if wiki is None:
            raise ValueError("UpdateIndex requires a Wiki")
        if not op.pages:
            raise ValueError("UpdateIndex requires op.pages to identify the orphan page")
        target_path = op.pages[0]
        existing = (wiki.wiki_dir / "index.md").read_text(encoding="utf-8")
        return PageUpdate(
            path="wiki/index.md",
            expected_sha256=_hash_text(existing),
            content=_update_index_body(existing, op.title, target_path),
            evidence=op.evidence,
        )
    if isinstance(op, AppendEvidence):
        return EvidenceAppend(
            path=op.path,
            expected_sha256=op.expected_sha256,
            content=op.content,
            evidence=op.evidence,
        )
    raise TypeError(f"unsupported repair op: {op!r}")


def from_repair_plan(plan: RepairPlan, wiki: Wiki | None = None) -> MemoryPlan:
    """Map a RepairPlan to a MemoryPlan.

    ``wiki`` is required for operations that derive replacement content
    from an existing page (AppendLink and UpdateIndex).

    Multiple ``AppendLink`` operations that target the same page are
    merged into a single ``PageUpdate`` so the underlying service can
    apply them atomically with one content rewrite per path. The
    ``MemoryPlan`` validator rejects two operations on the same path,
    so this grouping is required even when the original ``RepairPlan``
    interleaves AppendLinks with other ops (e.g. ``[AppendLink(a),
    CreateStub(x), AppendLink(a)]``).

    Non-AppendLink operations keep their original relative order and
    are emitted first. AppendLink groups are emitted afterward, one
    ``PageUpdate`` per unique ``append_to`` path.
    """
    operations: list[_PlanOperation] = []
    append_groups: dict[str, list[AppendLink]] = {}
    non_append_ops: list[_RepairOp] = []

    for op in plan.operations:
        if isinstance(op, AppendLink):
            append_groups.setdefault(op.append_to, []).append(op)
        else:
            non_append_ops.append(op)

    for op in non_append_ops:
        operations.append(_map_non_append_op(op, wiki))

    for path, links in append_groups.items():
        if wiki is None:
            raise ValueError("AppendLink requires a Wiki")
        operations.append(_merge_append_links(wiki, path, links))

    return MemoryPlan(
        operations=operations,
        rationale=plan.rationale,
        evidence=plan.evidence,
    )


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
