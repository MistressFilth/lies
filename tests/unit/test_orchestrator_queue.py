"""Unit tests for the orchestrator's EnrichmentQueue integration."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from pydantic_ai.models.test import TestModel

from lies.memory.enricher import MemoryEnricherDeps
from lies.memory.models import MemoryPlan, PageCreate, WikiLockBusy
from lies.memory.retry import EnrichmentQueue
from lies.orchestrator import Orchestrator


@pytest.fixture
def orchestrator(tmp_path: Path) -> Orchestrator:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    (root / ".lies" / "schema.md").write_text(
        "## Page types\n- concept\n- entity\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return Orchestrator(wiki_root=root, model=TestModel())


def test_orchestrator_instantiates_enrichment_queue(orchestrator: Orchestrator) -> None:
    assert isinstance(orchestrator._enrichment_queue, EnrichmentQueue)
    assert orchestrator._turn_counter == 0


def test_run_enrichment_enqueues_on_wiki_lock_busy(orchestrator: Orchestrator) -> None:
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/x.md",
                content="---\ntitle: X\ntype: concept\n---\n# X\n",
                evidence=["page-1"],
            )
        ],
        rationale="new",
        evidence=["page-1"],
    )

    with mock.patch.object(
        orchestrator, "_generate_memory_plan_from_deps", return_value=plan
    ), mock.patch.object(
        orchestrator._memory_service,
        "apply_plan",
        side_effect=WikiLockBusy("wiki memory lock is held by another process"),
    ):
        receipt = orchestrator._run_enrichment("ask", "answer", [], [])

    assert receipt.errors and receipt.errors[0].startswith(
        "queued_for_retry: WikiLockBusy:"
    )
    assert len(orchestrator._enrichment_queue) == 1
    queued = orchestrator._enrichment_queue._items[0]  # type: ignore[attr-defined]
    assert isinstance(queued.deps, MemoryEnricherDeps)
    assert queued.deps.answer == "answer"
    assert queued.last_reason.startswith("WikiLockBusy:")
