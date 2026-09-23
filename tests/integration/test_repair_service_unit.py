"""Unit tests for the apply_repair_plan service method."""

from __future__ import annotations

import subprocess  # noqa: F401  # kept for backward-compat with monkeypatched tests
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
from tests.conftest import make_wiki


@pytest.fixture(autouse=True)
def _stub_external_services(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub qmd + atomic_commit + snapshot envelope by default.

    ``apply_repair_plan`` routes through ``apply_plan`` which fires
    real ``qmd update`` + ``atomic_commit`` + snapshot/restore git
    subprocesses (~100ms each). The repair-service tests assert on
    wiki state, not on git/qmd, so the subprocesses are no-op'd by
    default. Tests that read real git history (``_tracked_porcelain``
    usage — currently none in this file) opt out by name.
    """
    from lies.memory.service import WikiMemoryService

    monkeypatch.setattr(WikiMemoryService, "_refresh_qmd", lambda self: (True, ""))
    monkeypatch.setattr(
        "lies.memory.service.atomic_commit",
        lambda *_a, **_kw: "deadbeef" + "0" * 32,
    )
    monkeypatch.setattr(
        WikiMemoryService,
        "_snapshot_working_tree",
        lambda _self, _repo: "fake-stash-ref",
    )
    monkeypatch.setattr(
        WikiMemoryService,
        "_restore_working_tree",
        lambda _self, _repo, _ref: None,
    )
    monkeypatch.setattr(
        WikiMemoryService,
        "_discard_snapshot",
        lambda _self, _repo, _ref: None,
    )


@pytest.fixture
def git_wiki(tmp_path: Path):
    """Wiki rooted at ``tmp_path/wiki`` with a real ``git init`` baseline.

    The autouse ``_stub_external_services`` fixture stubs
    ``atomic_commit`` + snapshot envelope + qmd refresh, so the apply
    path never shells out from inside ``apply_repair_plan``. The
    baseline ``git init + config + add + commit`` here is needed because
    two tests (``test_apply_repair_plan_rejects_hash_mismatch``,
    ````test_apply_repair_plan_update_index_adds_orphan_to_catalog``)
    perform their own ``git add . && git commit`` to seed a page
    before invoking the repair path; without a real repo those
    seeding subprocesses fail.
    """
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return make_wiki(name="repair-test", data_root=root)


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


def test_from_repair_plan_maps_append_link_to_page_update(git_wiki) -> None:
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
    memory_plan = from_repair_plan(plan, wiki=git_wiki)
    op = memory_plan.operations[0]
    assert isinstance(op, PageUpdate)
    assert op.path == "concepts/a.md"
    assert op.expected_sha256
    assert "[B](concepts/b.md)" in op.content


def test_from_repair_plan_maps_update_index_to_page_update(git_wiki) -> None:
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
    memory_plan = from_repair_plan(plan, wiki=git_wiki)
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


@pytest.mark.slow
def test_apply_repair_plan_creates_stub_page(git_wiki) -> None:
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


def test_apply_repair_plan_rejects_path_escape(git_wiki) -> None:
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


def test_apply_repair_plan_rejects_hash_mismatch(git_wiki) -> None:
    page = git_wiki.wiki_dir / "concepts" / "x.md"
    page.write_text("---\ntitle: X\ntype: concept\n---\n# X\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=git_wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=git_wiki.data_root, check=True)
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


@pytest.mark.slow
def test_apply_repair_plan_update_index_adds_orphan_to_catalog(git_wiki) -> None:
    orphan = git_wiki.wiki_dir / "concepts" / "orphan.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("---\ntitle: Orphan\ntype: concept\n---\n# Orphan\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=git_wiki.data_root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=git_wiki.data_root, check=True)

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
    index_content = (git_wiki.wiki_dir / "index.md").read_text(encoding="utf-8")
    assert "concepts/orphan.md" in index_content
    assert "wiki/index.md" not in index_content.rstrip().splitlines()[-1]
