"""JSONL receipt sidecar at <wiki>/.lies/memory_plans.jsonl.

Each applied `MemoryPlan` from `WikiMemoryService.apply_plan` writes one
line. Git log is authoritative; the sidecar is rebuildable via
`reconcile_from_git_log`. Append ordering inside `apply_plan` is
commit → append → qmd refresh; idempotency keyed on `commit_sha`.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from lies.memory.models import MemoryPlan, MemoryPlanRecord
from lies.wiki.wiki import Wiki

log = logging.getLogger(__name__)

_RATIONALE_MAX = 120
_PAGES_MAX = 8


def _sidecar_path(wiki: Wiki) -> Path:
    """Resolve `<wiki.data_root>/.lies/memory_plans.jsonl`."""
    return wiki.data_root / ".lies" / "memory_plans.jsonl"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rationale_truncated(text: str) -> str:
    if len(text) <= _RATIONALE_MAX:
        return text
    return text[:_RATIONALE_MAX] + "…"


def _pages_capped(paths: list[str]) -> list[str]:
    if len(paths) <= _PAGES_MAX:
        return list(paths)
    return list(paths[: _PAGES_MAX - 1]) + [f"+{len(paths) - _PAGES_MAX} more"]


def _ops_histogram(plan: MemoryPlan) -> dict[str, int]:
    counts = Counter(op.kind.value for op in plan.operations)
    return dict(sorted(counts.items()))


def _is_duplicate(wiki: Wiki, commit_sha: str) -> bool:
    """Idempotency check: skip append when the last line already has this SHA."""
    path = _sidecar_path(wiki)
    if not path.exists():
        return False
    last_line = ""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                last_line = line
    if not last_line:
        return False
    try:
        record = json.loads(last_line)
    except json.JSONDecodeError:
        return False
    return record.get("commit_sha") == commit_sha


def append_receipt(
    wiki: Wiki,
    plan: MemoryPlan,
    commit_sha: str,
    *,
    evidence_count: int = 0,
) -> None:
    """Append one JSONL line. Idempotent on `commit_sha`. Never raises.

    Logs to stderr on filesystem failure so the orchestrator's
    `MemoryReceipt.errors` can pick the failure up without crashing the
    turn. Callers should still surface `errors=[sidecar_append_failed]`
    in the receipt when `append_receipt` returns False via `_append_failed`.
    """
    path = _sidecar_path(wiki)
    if _is_duplicate(wiki, commit_sha):
        return
    pages = [op.path for op in plan.operations]
    record = MemoryPlanRecord(
        ts=_now_iso(),
        commit_sha=commit_sha,
        rationale=_rationale_truncated(plan.rationale),
        pages=_pages_capped(pages),
        ops=_ops_histogram(plan),
        evidence_count=evidence_count,
    )
    _ensure_parent(path)
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")
    except OSError as exc:
        log.warning("sidecar append failed: %s", exc)
        print(f"sidecar_append_failed: {exc}", file=sys.stderr)
