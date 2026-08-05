"""Structural validation for repair_agent output.

Catches the cases the prompt's HARD RULE doesn't: ops for out-of-range
findings, ops against ``safe_to_fix=False`` findings, ops whose pages
don't intersect the referenced finding's pages, ops that target
non-existent pages, and ``UpdateIndex`` ops for pages already listed in
``wiki/index.md``.

Rules 1-4 raise :class:`WikiPlanInvalid` (atomic rejection: no
filesystem write). Rule 5 silently drops redundant ``UpdateIndex``
operations and records the original indices in ``dropped_ops``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from lies.agents.linter import LintFinding
from lies.agents.repair_models import (
    AppendEvidence,
    AppendLink,
    CreateStub,
    RepairPlan,
    UpdateIndex,
)
from lies.memory.validation import validate_page_path
from lies.wiki.layout import WikiLayout


def _index_listed_paths(index_path: Path) -> set[str]:
    """Return the set of wiki-dir-relative paths already in ``index_path``.

    Parses the catalog body line by line; any ``[Title](path)`` link
    whose target is a relative path (no scheme) is counted as listed.
    """
    listed: set[str] = set()
    if not index_path.exists():
        return listed
    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for line in index_path.read_text(encoding="utf-8").splitlines():
        for match in pattern.finditer(line):
            target = match.group(2).strip()
            if "://" in target or target.startswith("#"):
                continue
            listed.add(target)
    return listed


@dataclass(frozen=True)
class ValidatedRepairPlan:
    """A repair plan after structural validation.

    ``dropped_ops`` holds the original indices of ``UpdateIndex``
    operations that were filtered because the target page was already
    listed in ``wiki/index.md``. The remaining ``plan.operations`` is
    the post-drop set, ready for the memory envelope.
    """

    plan: RepairPlan
    dropped_ops: tuple[int, ...] = ()


def validate_plan(
    plan: RepairPlan,
    layout: WikiLayout,
    findings: list[LintFinding],
) -> ValidatedRepairPlan:
    """Validate ``plan`` against the wiki layout and the lint findings."""
    from lies.memory.models import WikiPlanInvalid

    if not plan.operations:
        return ValidatedRepairPlan(plan=plan, dropped_ops=())

    for index, op in enumerate(plan.operations):
        finding_index = op.finding_index
        # Rule 1: bounds check.
        if not (0 <= finding_index < len(findings)):
            raise WikiPlanInvalid(
                f"op {type(op).__name__} finding_index {finding_index} out of range "
                f"({len(findings)} findings)",
                path=getattr(op, "path", None),
            )
        # Rule 2: safety check.
        finding = findings[finding_index]
        if not finding.safe_to_fix:
            raise WikiPlanInvalid(
                f"op {type(op).__name__} targets finding {finding_index} "
                f"({finding.category}) whose safe_to_fix is False",
                path=getattr(op, "path", None),
            )
        # Rule 3: op pages must intersect the referenced finding's pages.
        op_pages = set(op.pages)
        finding_pages = set(finding.pages)
        if op_pages and not (op_pages & finding_pages):
            raise WikiPlanInvalid(
                f"op {type(op).__name__} pages {sorted(op_pages)} do not "
                f"intersect finding {finding_index} pages {sorted(finding_pages)}",
                path=getattr(op, "path", None),
            )
        # Rule 4: per-op filesystem checks.
        if isinstance(op, CreateStub):
            resolved = validate_page_path(layout, op.path)
            if resolved.exists():
                raise WikiPlanInvalid(
                    f"CreateStub: path {op.path!r} already exists",
                    path=op.path,
                )
        elif isinstance(op, AppendLink):
            target = validate_page_path(layout, op.target_path)
            if not target.exists():
                raise WikiPlanInvalid(
                    f"AppendLink: target_path {op.target_path!r} does not exist; "
                    f"use CreateStub instead",
                    path=op.target_path,
                )
            host = validate_page_path(layout, op.append_to)
            if not host.exists():
                raise WikiPlanInvalid(
                    f"AppendLink: append_to {op.append_to!r} does not exist",
                    path=op.append_to,
                )
        elif isinstance(op, AppendEvidence):
            resolved = validate_page_path(layout, op.path)
            if not resolved.exists():
                raise WikiPlanInvalid(
                    f"AppendEvidence: path {op.path!r} does not exist",
                    path=op.path,
                )
        # UpdateIndex is handled by rule 5 below.

    # Rule 5: drop redundant UpdateIndex ops.
    listed = _index_listed_paths(layout.index_path)
    kept: list = []
    dropped: list[int] = []
    for index, op in enumerate(plan.operations):
        if isinstance(op, UpdateIndex) and op.pages and op.pages[0] in listed:
            dropped.append(index)
            continue
        kept.append(op)

    if dropped == list(range(len(plan.operations))):
        # All ops were redundant; the plan is a noop. Keep the plan
        # shell so downstream callers see a valid object.
        filtered_plan = plan.model_copy(update={"operations": []})
    elif dropped:
        filtered_plan = plan.model_copy(update={"operations": kept})
    else:
        filtered_plan = plan

    return ValidatedRepairPlan(
        plan=filtered_plan,
        dropped_ops=tuple(dropped),
    )
