"""JSONL receipt sidecar at <wiki>/.lies/memory_plans.jsonl.

Each applied `MemoryPlan` from `WikiMemoryService.apply_plan` writes one
line. Git log is authoritative; the sidecar is rebuildable via
`reconcile_from_git_log`. Append ordering inside `apply_plan` is
commit → append → qmd refresh; idempotency keyed on `commit_sha`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

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


def _parse_line(line: str) -> MemoryPlanRecord | None:
    try:
        return MemoryPlanRecord.model_validate_json(line)
    except ValidationError:
        return None


def read_recent(
    wiki: Wiki,
    limit: int = 10,
    *,
    page: str | None = None,
    op: str | None = None,
    since: str | None = None,
) -> list[MemoryPlanRecord]:
    """Read the last `limit` records, optionally filtered. Empty on miss."""
    path = _sidecar_path(wiki)
    if not path.exists():
        return []
    rows: list[MemoryPlanRecord] = []
    skipped = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            record = _parse_line(line.strip())
            if record is None:
                skipped += 1
                continue
            if page is not None and not any(page in p for p in record.pages):
                continue
            if op is not None and op not in record.ops:
                continue
            if since is not None and record.ts < since:
                continue
            rows.append(record)
    if skipped:
        log.info("sidecar: skipped %d malformed line(s)", skipped)
    return rows[-limit:]


_RATIONALE_RE = re.compile(r"^memory:\s*(.*?)\s*$", re.MULTILINE)


def _git_log_memory_commits(data_root: Path) -> list[tuple[str, str, str]]:
    """Return list of (sha, iso_ts, rationale) for `memory:` commits.

    Uses `git log --grep='^memory:'` to filter; ignores non-matching commits.
    """
    out = subprocess.run(
        [
            "git",
            "-C",
            str(data_root),
            "log",
            "--grep=^memory:",
            "--format=%H%x00%aI%x00%s",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return []
    rows = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        sha, ts, subject = line.split("\x00", 2)
        m = _RATIONALE_RE.match(subject)
        rationale = m.group(1) if m else subject
        rows.append((sha, ts, rationale))
    return rows


def _body_pages_ops_evidence(
    commit_sha: str, data_root: Path
) -> tuple[list[str], dict[str, int], int]:
    """Extract Pages/Ops/Evidence trailers from a commit message body."""
    out = subprocess.run(
        [
            "git",
            "-C",
            str(data_root),
            "log",
            "-1",
            "--format=%B",
            commit_sha,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    body = out.stdout if out.returncode == 0 else ""
    pages: list[str] = []
    ops: dict[str, int] = {}
    evidence = 0
    for line in body.splitlines():
        if line.startswith("Pages:"):
            pages = [p.strip() for p in line.removeprefix("Pages:").split(",") if p.strip()]
        elif line.startswith("Ops:"):
            for tok in line.removeprefix("Ops:").split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    try:
                        ops[k] = int(v)
                    except ValueError:
                        pass
        elif line.startswith("Evidence:"):
            try:
                evidence = int(line.removeprefix("Evidence:").strip())
            except ValueError:
                evidence = 0
    return pages, ops, evidence


def reconcile_from_git_log(wiki: Wiki) -> int:
    """Rewrite `<wiki>/.lies/memory_plans.jsonl` from `git log --grep='^memory:'`.

    Returns the number of rows written. Malformed commits (no Pages/Ops
    trailers in the body) are skipped with a stderr warning.
    """
    rows: list[MemoryPlanRecord] = []
    skipped = 0
    for sha, ts, rationale in _git_log_memory_commits(wiki.data_root):
        pages, ops, evidence = _body_pages_ops_evidence(sha, wiki.data_root)
        if not pages:
            skipped += 1
            log.warning("sidecar reconcile: skipping %s (no Pages trailer)", sha[:12])
            continue
        rows.append(
            MemoryPlanRecord(
                ts=ts,
                commit_sha=sha,
                rationale=_rationale_truncated(rationale),
                pages=_pages_capped(pages),
                ops=ops,
                evidence_count=evidence,
            )
        )
    path = _sidecar_path(wiki)
    _ensure_parent(path)
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 - explicit close after fsync
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        for r in rows:
            tmp.write(r.model_dump_json() + "\n")
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.replace(tmp.name, path)
    if skipped:
        print(f"sidecar reconcile: skipped {skipped} malformed commit(s)", file=sys.stderr)
    return len(rows)


def truncate(wiki: Wiki, keep: int, *, force: bool = False) -> int:
    """Cap the sidecar to its last `keep` lines. Atomic rewrite.

    Refuse `keep <= 0`. Refuse `keep > current_count` unless `force=True`.
    Returns the count kept.
    """
    if keep <= 0:
        raise ValueError("--keep must be positive")
    path = _sidecar_path(wiki)
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as fh:
        all_lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    if keep > len(all_lines) and not force:
        raise ValueError(
            f"--keep > current count (asked {keep}, have {len(all_lines)}); pass --force to allow"
        )
    kept = all_lines[-keep:]
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 - explicit close after fsync
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        for ln in kept:
            tmp.write(ln + "\n")
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.replace(tmp.name, path)
    return len(kept)
