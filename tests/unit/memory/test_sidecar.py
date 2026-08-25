"""Unit tests for the JSONL receipt sidecar at <wiki>/.lies/memory_plans.jsonl."""

from __future__ import annotations

import json
from pathlib import Path

from lies.memory import sidecar
from lies.memory.models import MemoryPlan, PageCreate
from lies.wiki.wiki import Wiki


def _wiki(tmp_path: Path) -> Wiki:
    """Build a minimal Wiki rooted at ``tmp_path``; only ``data_root`` matters.

    ``data_root`` is what ``append_receipt`` resolves the sidecar against.
    The other four XDG role roots are pointed at sibling subdirs under
    ``tmp_path`` so the dataclass is constructible; the sidecar code never
    reads them.
    """
    return Wiki(
        name="t",
        data_root=tmp_path,
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )


def test_append_receipt_writes_one_jsonl_line(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    plan = MemoryPlan(
        rationale="create entity page for Postgres",
        operations=[
            PageCreate(path="wiki/entities/postgres.md", content="# Postgres", evidence=["page-1"]),
            PageCreate(path="wiki/concepts/schemas.md", content="# Schemas", evidence=["page-2"]),
        ],
        evidence=["page-1", "page-2"],
    )
    sidecar.append_receipt(wiki, plan, commit_sha="a1b2c3d4e5f6" + "0" * 28)
    sidecar_path = tmp_path / ".lies" / "memory_plans.jsonl"
    assert sidecar_path.exists()
    lines = sidecar_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["commit_sha"] == "a1b2c3d4e5f6" + "0" * 28
    assert record["rationale"] == "create entity page for Postgres"
    assert record["pages"] == ["wiki/entities/postgres.md", "wiki/concepts/schemas.md"]
    assert record["ops"] == {"create": 2}
    assert record["evidence_count"] == 0
    assert "ts" in record


def test_append_receipt_is_idempotent_on_commit_sha(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    plan = MemoryPlan(
        rationale="noop",
        operations=[
            PageCreate(path="wiki/entities/postgres.md", content="# Postgres", evidence=["page-1"]),
        ],
        evidence=["page-1"],
    )
    sha = "deadbeef" + "0" * 32
    sidecar.append_receipt(wiki, plan, commit_sha=sha)
    sidecar.append_receipt(wiki, plan, commit_sha=sha)
    sidecar_path = tmp_path / ".lies" / "memory_plans.jsonl"
    lines = sidecar_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_append_receipt_caps_pages_at_eight(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    ops = [
        PageCreate(path=f"wiki/entities/p{i}.md", content=f"# P{i}", evidence=["page-1"])
        for i in range(12)
    ]
    plan = MemoryPlan(rationale="bulk create", operations=ops, evidence=["page-1"])
    sidecar.append_receipt(wiki, plan, commit_sha="cafef00d" + "0" * 32)
    sidecar_path = tmp_path / ".lies" / "memory_plans.jsonl"
    record = json.loads(sidecar_path.read_text().splitlines()[0])
    assert len(record["pages"]) == 8
    assert record["pages"][-1] == "+4 more"


def test_append_receipt_truncates_rationale(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    long_rationale = "x" * 200
    plan = MemoryPlan(
        rationale=long_rationale,
        operations=[
            PageCreate(path="wiki/entities/p.md", content="# P", evidence=["page-1"]),
        ],
        evidence=["page-1"],
    )
    sidecar.append_receipt(wiki, plan, commit_sha="f" * 40)
    sidecar_path = tmp_path / ".lies" / "memory_plans.jsonl"
    record = json.loads(sidecar_path.read_text().splitlines()[0])
    assert len(record["rationale"]) == 121  # 120 chars + ellipsis
    assert record["rationale"].endswith("…")


def _seed_three_rows(wiki) -> None:
    """Seed three plans, each touching two pages: one entity, one concept."""
    plans = [
        MemoryPlan(
            rationale=f"plan {i}",
            operations=[
                PageCreate(path=f"wiki/entities/p{i}.md", content=f"# P{i}", evidence=["page-1"]),
                PageCreate(path=f"wiki/concepts/c{i}.md", content=f"# C{i}", evidence=["page-1"]),
            ],
            evidence=["page-1"],
        )
        for i in range(3)
    ]
    for i, plan in enumerate(plans):
        sidecar.append_receipt(wiki, plan, commit_sha=f"{i:040x}")


def test_read_recent_returns_last_n(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    _seed_three_rows(wiki)
    rows = sidecar.read_recent(wiki, limit=2)
    assert len(rows) == 2
    assert rows[0].rationale == "plan 1"
    assert rows[1].rationale == "plan 2"


def test_read_recent_filters_by_page_substring(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    _seed_three_rows(wiki)
    rows = sidecar.read_recent(wiki, limit=10, page="entities/p1.md")
    assert len(rows) == 1
    assert rows[0].rationale == "plan 1"


def test_read_recent_filters_by_op(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    _seed_three_rows(wiki)
    rows = sidecar.read_recent(wiki, limit=10, op="create")
    assert len(rows) == 3


def test_read_recent_filters_by_since(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    _seed_three_rows(wiki)
    rows = sidecar.read_recent(wiki, limit=10, since="2099-01-01T00:00:00Z")
    assert rows == []


def test_read_recent_returns_empty_on_missing_file(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    assert sidecar.read_recent(wiki, limit=10) == []


def test_read_recent_skips_malformed_lines(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path)
    sidecar_path = tmp_path / ".lies" / "memory_plans.jsonl"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(
        '{"ts":"2026-01-01T00:00:00Z","commit_sha":"a","rationale":"ok","pages":[],"ops":{"create":1},"evidence_count":0}\n'
        "this is not json\n",
        encoding="utf-8",
    )
    rows = sidecar.read_recent(wiki, limit=10)
    assert len(rows) == 1
