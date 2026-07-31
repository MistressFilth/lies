"""Map a RepairPlan onto existing WikiMemoryService operations.

The 4 repair primitives (CreateStub, AppendLink, UpdateIndex,
AppendEvidence) are translated into the existing PageCreate /
PageUpdate / EvidenceAppend memory operations. apply_repair_plan then
flows through the same flock, atomic_commit, and qmd refresh envelope
as apply_plan.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from lies.agents.repair_models import (
    AppendEvidence,
    AppendLink,
    CreateStub,
    RepairPlan,
    UpdateIndex,
)
from lies.memory.models import (
    EvidenceAppend,
    MemoryPlan,
    PageCreate,
    PageUpdate,
    _PlanOperation,
)
from lies.wiki.layout import WikiLayout


def _stub_body(title: str) -> str:
    today = datetime.now(tz=timezone.utc).date().isoformat()
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


def _append_link_body(
    existing: str, link_text: str, target_path: str, anchor: str = ""
) -> str:
    """Append a markdown link to the end of existing page content."""
    anchor_part = f"#{anchor}" if anchor else ""
    link = f"[{link_text}]({target_path}{anchor_part})"
    return existing.rstrip() + "\n\n" + link + "\n"


def _update_index_body(existing: str, title: str, path: str) -> str:
    """Append a catalog entry to wiki/index.md."""
    return existing.rstrip() + f"\n- [{title}]({path})\n"


def from_repair_plan(
    plan: RepairPlan, layout: WikiLayout | None = None
) -> MemoryPlan:
    """Map a RepairPlan to a MemoryPlan.

    ``layout`` is required for operations that derive replacement content
    from an existing page (AppendLink and UpdateIndex).
    """
    operations: list[_PlanOperation] = []
    for op in plan.operations:
        if isinstance(op, CreateStub):
            operations.append(
                PageCreate(
                    path=op.path,
                    content=_stub_body(op.title),
                    evidence=op.evidence,
                )
            )
        elif isinstance(op, AppendLink):
            if layout is None:
                raise ValueError("AppendLink requires a WikiLayout")
            existing = (layout.wiki_dir / op.append_to).read_text(encoding="utf-8")
            operations.append(
                PageUpdate(
                    path=op.append_to,
                    expected_sha256=_hash_text(existing),
                    content=_append_link_body(
                        existing, op.link_text, op.target_path, op.anchor
                    ),
                    evidence=op.evidence,
                )
            )
        elif isinstance(op, UpdateIndex):
            if layout is None:
                raise ValueError("UpdateIndex requires a WikiLayout")
            existing = layout.index_path.read_text(encoding="utf-8")
            operations.append(
                PageUpdate(
                    path=op.path,
                    expected_sha256=_hash_text(existing),
                    content=_update_index_body(existing, op.title, op.path),
                    evidence=op.evidence,
                )
            )
        elif isinstance(op, AppendEvidence):
            operations.append(
                EvidenceAppend(
                    path=op.path,
                    expected_sha256=op.expected_sha256,
                    content=op.content,
                    evidence=op.evidence,
                )
            )
        else:
            raise TypeError(f"unsupported repair op: {op!r}")
    return MemoryPlan(
        operations=operations,
        rationale=plan.rationale,
        evidence=plan.evidence,
    )


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
