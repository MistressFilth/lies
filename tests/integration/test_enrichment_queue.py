"""End-to-end tests for the EnrichmentQueue integration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from pydantic_ai.models.test import TestModel

from lies.memory.models import MemoryPlan, PageCreate, WikiLockBusy
from lies.orchestrator import Orchestrator
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki


@pytest.fixture
def wiki(tmp_path: Path) -> Wiki:
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    w = make_wiki(name="enrich", data_root=root)
    w.config_root.mkdir(parents=True, exist_ok=True)
    (w.config_root / "schema.md").write_text(
        "## Page types\n- concept\n- entity\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return w


def _create_plan() -> MemoryPlan:
    return MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/example.md",
                content="---\ntitle: Example\ntype: concept\n---\n# Example\n",
                evidence=["page-1"],
            )
        ],
        rationale="new concept",
        evidence=["page-1"],
    )


def test_queued_retry_succeeds_on_next_turn(wiki: Wiki) -> None:
    orch = Orchestrator(wiki=wiki, model=TestModel())
    orch._memory_service.register_evidence({"page-1"})
    call_count = {"n": 0}

    def fake_apply(plan: MemoryPlan) -> object:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise WikiLockBusy("held")
        from lies.memory.models import MemoryReceipt, OperationKind, PageReference

        (wiki.wiki_dir / "concepts").mkdir(parents=True, exist_ok=True)
        (wiki.wiki_dir / "concepts" / "example.md").write_text(
            plan.operations[0].content, encoding="utf-8"
        )
        return MemoryReceipt(
            changed_pages=[
                PageReference(
                    path="concepts/example.md", collection_id="wiki", op=OperationKind.CREATE
                )
            ],
            deferred=[],
            fallback_used=False,
            fallback_reason="",
            errors=[],
        )

    with (
        mock.patch.object(orch, "_generate_memory_plan_from_deps", return_value=_create_plan()),
        mock.patch.object(orch._memory_service, "apply_plan", side_effect=fake_apply),
    ):
        orch._turn_counter += 1
        orch._run_enrichment("ask", "answer", [], [])
        assert len(orch._enrichment_queue) == 1
        orch._turn_counter += 1
        orch._enrichment_queue.drain(
            enrich_fn=orch._generate_memory_plan_from_deps,
            apply_fn=orch._memory_service.apply_plan,
        )

    assert len(orch._enrichment_queue) == 0
    assert (wiki.wiki_dir / "concepts" / "example.md").exists()


def test_queued_retry_hits_cap_after_three_failures(wiki: Wiki) -> None:
    orch = Orchestrator(wiki=wiki, model=TestModel())
    orch._memory_service.register_evidence({"page-1"})

    def always_locked(plan: MemoryPlan) -> object:
        raise WikiLockBusy("held")

    with (
        mock.patch.object(orch, "_generate_memory_plan_from_deps", return_value=_create_plan()),
        mock.patch.object(orch._memory_service, "apply_plan", side_effect=always_locked),
    ):
        orch._turn_counter += 1
        orch._run_enrichment("ask", "answer", [], [])
        for _ in range(3):
            orch._turn_counter += 1
            orch._enrichment_queue.drain(
                enrich_fn=orch._generate_memory_plan_from_deps,
                apply_fn=orch._memory_service.apply_plan,
            )

    assert len(orch._enrichment_queue) == 0
    lines = orch._enrichment_queue.format_receipt_lines()
    assert len(lines) == 1
    assert "deferred after 3 attempts" in lines[0]


def test_queue_is_per_orchestrator_instance(wiki: Wiki) -> None:
    orch_a = Orchestrator(wiki=wiki, model=TestModel())
    orch_b = Orchestrator(wiki=wiki, model=TestModel())
    assert orch_a._enrichment_queue is not orch_b._enrichment_queue


def test_queued_item_becomes_noop_on_reenrichment(wiki: Wiki) -> None:
    """A queued item that produces noop on the next drain drops silently.

    Scenario: a plan is queued for retry. On the next turn, the
    enricher produces a noop (MemoryPlan with empty operations). The
    drain should drop the item, leave the queue empty, and emit no
    errors or deferred lines.
    """
    from lies.memory.models import MemoryPlan

    orch = Orchestrator(wiki=wiki, model=TestModel())
    orch._memory_service.register_evidence({"page-1"})

    # Enqueue an item by simulating a WikiLockBusy at apply time.
    with (
        mock.patch.object(orch, "_generate_memory_plan_from_deps", return_value=_create_plan()),
        mock.patch.object(
            orch._memory_service,
            "apply_plan",
            side_effect=WikiLockBusy("held"),
        ),
    ):
        orch._turn_counter += 1
        orch._run_enrichment("ask", "answer", [], [])

    assert len(orch._enrichment_queue) == 1

    # Drain: the enricher now returns a noop plan. The queue should
    # drop the item silently (no errors, no deferred lines).
    noop = MemoryPlan(operations=[], rationale="nothing to file", evidence=[])
    with mock.patch.object(orch, "_generate_memory_plan_from_deps", return_value=noop):
        orch._turn_counter += 1
        result = orch._enrichment_queue.drain(
            enrich_fn=orch._generate_memory_plan_from_deps,
            apply_fn=orch._memory_service.apply_plan,
        )

    assert len(orch._enrichment_queue) == 0
    assert result.still_queued == 0
    assert result.deferred == []
    assert result.applied == []
    assert orch._enrichment_queue.format_receipt_lines() == []
    # No file was created on disk since the noop plan never applied.
    assert not (wiki.wiki_dir / "concepts" / "example.md").exists()
