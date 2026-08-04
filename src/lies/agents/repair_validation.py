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

from dataclasses import dataclass

from lies.agents.linter import LintFinding
from lies.agents.repair_models import RepairPlan
from lies.wiki.layout import WikiLayout


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
    """Validate ``plan`` against the wiki layout and the lint findings.

    See module docstring for the rules. Returns a
    :class:`ValidatedRepairPlan`; raises :class:`WikiPlanInvalid` on any
    rule 1-4 violation.
    """
    if not plan.operations:
        return ValidatedRepairPlan(plan=plan, dropped_ops=())
    # Validation rules are wired in subsequent tasks; this skeleton
    # returns the plan unchanged so the empty-operations test passes.
    return ValidatedRepairPlan(plan=plan, dropped_ops=())
