"""Unit tests for the apply_repair_plan service method."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lies.agents.repair_models import (
    AppendEvidence,
    AppendLink,
    CreateStub,
    RepairPlan,
    UpdateIndex,
)
from lies.memory.models import (
    EvidenceAppend,
    PageCreate,
    PageUpdate,
    WikiPlanInvalid,
    WikiWriteConflict,
)
from lies.memory.repair import from_repair_plan
from lies.memory.service import WikiMemoryService
from lies.wiki.layout import WikiLayout


@pytest.fixture
def git_wiki(tmp_path: Path) -> WikiLayout:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return WikiLayout(root)


def test_from_repair_plan_maps_create_stub_to_page_create() -> None:
    plan = RepairPlan(
        operations=[
            CreateStub(
                path="concepts/new.md",
                title="New",
                finding_index=0,
                pages=[],
                rationale="new",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    memory_plan = from_repair_plan(plan)
    assert len(memory_plan.operations) == 1
    op = memory_plan.operations[0]
    assert isinstance(op, PageCreate)
    assert op.path == "concepts/new.md"
    assert "Stub" in op.content


def test_from_repair_plan_maps_append_link_to_page_update(git_wiki: WikiLayout) -> None:
    page = git_wiki.wiki_dir / "concepts" / "a.md"
    page.write_text("# A\n", encoding="utf-8")
    plan = RepairPlan(
        operations=[
            AppendLink(
                target_path="concepts/b.md",
                link_text="B",
                append_to="concepts/a.md",
                finding_index=0,
                pages=["concepts/a.md"],
                rationale="xref",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    memory_plan = from_repair_plan(plan, layout=git_wiki)
    op = memory_plan.operations[0]
    assert isinstance(op, PageUpdate)
    assert op.path == "concepts/a.md"
    assert op.expected_sha256
    assert "[B](concepts/b.md)" in op.content


def test_from_repair_plan_maps_update_index_to_page_update(git_wiki: WikiLayout) -> None:
    plan = RepairPlan(
        operations=[
            UpdateIndex(
                path="wiki/index.md",
                title="X",
                finding_index=0,
                pages=["concepts/x.md"],
                rationale="orphan",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    memory_plan = from_repair_plan(plan, layout=git_wiki)
    op = memory_plan.operations[0]
    assert isinstance(op, PageUpdate)
    assert op.path == "wiki/index.md"


def test_from_repair_plan_maps_append_evidence_to_evidence_append() -> None:
    plan = RepairPlan(
        operations=[
            AppendEvidence(
                path="concepts/x.md",
                expected_sha256="abc123",
                content="## Note",
                finding_index=0,
                pages=["concepts/x.md"],
                rationale="evidence",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    memory_plan = from_repair_plan(plan)
    op = memory_plan.operations[0]
    assert isinstance(op, EvidenceAppend)
    assert op.expected_sha256 == "abc123"


def test_apply_repair_plan_creates_stub_page(git_wiki: WikiLayout) -> None:
    plan = RepairPlan(
        operations=[
            CreateStub(
                path="concepts/example.md",
                title="Example",
                finding_index=0,
                pages=[],
                rationale="new",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    service = WikiMemoryService(git_wiki)
    receipt = service.apply_repair_plan(plan)
    assert receipt.changed_pages
    assert (git_wiki.wiki_dir / "concepts" / "example.md").exists()


def test_apply_repair_plan_rejects_path_escape(git_wiki: WikiLayout) -> None:
    plan = RepairPlan(
        operations=[
            CreateStub(
                path="../outside.md",
                title="Outside",
                finding_index=0,
                pages=[],
                rationale="escape",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    service = WikiMemoryService(git_wiki)
    with pytest.raises(WikiPlanInvalid):
        service.apply_repair_plan(plan)


def test_apply_repair_plan_rejects_hash_mismatch(git_wiki: WikiLayout) -> None:
    page = git_wiki.wiki_dir / "concepts" / "x.md"
    page.write_text("---\ntitle: X\ntype: concept\n---\n# X\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=git_wiki.root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=git_wiki.root, check=True)
    plan = RepairPlan(
        operations=[
            AppendEvidence(
                path="concepts/x.md",
                expected_sha256="0" * 64,
                content="## Note",
                finding_index=0,
                pages=["concepts/x.md"],
                rationale="hash",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    service = WikiMemoryService(git_wiki)
    with pytest.raises(WikiWriteConflict):
        service.apply_repair_plan(plan)


def test_apply_repair_plan_update_index_adds_orphan_to_catalog(git_wiki: WikiLayout) -> None:
    orphan = git_wiki.wiki_dir / "concepts" / "orphan.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("---\ntitle: Orphan\ntype: concept\n---\n# Orphan\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=git_wiki.root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=git_wiki.root, check=True)

    plan = RepairPlan(
        operations=[
            UpdateIndex(
                path="wiki/index.md",
                title="Orphan",
                finding_index=0,
                pages=["concepts/orphan.md"],
                rationale="orphan",
                evidence=["f0"],
            ),
        ],
        rationale="r",
        evidence=["f0"],
    )
    service = WikiMemoryService(git_wiki)
    receipt = service.apply_repair_plan(plan)
    assert receipt.changed_pages
    index_content = git_wiki.index_path.read_text(encoding="utf-8")
    assert "concepts/orphan.md" in index_content
    assert "wiki/index.md" not in index_content.rstrip().splitlines()[-1]
