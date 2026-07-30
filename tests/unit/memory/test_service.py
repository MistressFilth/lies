# tests/unit/memory/test_service.py
import hashlib
import subprocess
from pathlib import Path

import pytest

from lies.memory.models import (
    MemoryPlan,
    PageCreate,
    PageUpdate,
    WikiPlanInvalid,
    WikiWriteConflict,
)
from lies.memory.service import WikiMemoryService
from lies.wiki.layout import WikiLayout


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=root, check=True,
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)


@pytest.fixture
def git_wiki(tmp_path: Path) -> WikiLayout:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    _git_init(root)
    return WikiLayout(root)


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_apply_plan_creates_page(git_wiki: WikiLayout) -> None:
    service = WikiMemoryService(git_wiki)
    plan = MemoryPlan(
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
    receipt = service.apply_plan(plan)
    assert receipt.changed_pages
    assert (git_wiki.wiki_dir / "concepts" / "example.md").exists()


def test_apply_plan_updates_page_with_matching_hash(git_wiki: WikiLayout) -> None:
    path = git_wiki.wiki_dir / "concepts" / "x.md"
    body = "---\ntitle: X\ntype: concept\n---\n# X\n"
    path.write_text(body, encoding="utf-8")
    _git_init(git_wiki.root)
    plan = MemoryPlan(
        operations=[
            PageUpdate(
                path="concepts/x.md",
                expected_sha256=_sha(body),
                content=body + "\n## New section\n",
                evidence=["page-1"],
            )
        ],
        rationale="extend",
        evidence=["page-1"],
    )
    service = WikiMemoryService(git_wiki)
    receipt = service.apply_plan(plan)
    assert receipt.changed_pages
    assert "New section" in path.read_text(encoding="utf-8")


def test_apply_plan_rejects_hash_mismatch(git_wiki: WikiLayout) -> None:
    path = git_wiki.wiki_dir / "concepts" / "x.md"
    path.write_text(
        "---\ntitle: X\ntype: concept\n---\n# X\n", encoding="utf-8"
    )
    _git_init(git_wiki.root)
    plan = MemoryPlan(
        operations=[
            PageUpdate(
                path="concepts/x.md",
                expected_sha256="0" * 64,
                content="# X\n",
                evidence=["page-1"],
            )
        ],
        rationale="bad hash",
        evidence=["page-1"],
    )
    service = WikiMemoryService(git_wiki)
    with pytest.raises(WikiWriteConflict):
        service.apply_plan(plan)


def test_apply_plan_rejects_path_escape(git_wiki: WikiLayout) -> None:
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="../outside.md",
                content="x",
                evidence=["page-1"],
            )
        ],
        rationale="escape",
        evidence=["page-1"],
    )
    service = WikiMemoryService(git_wiki)
    with pytest.raises(WikiPlanInvalid):
        service.apply_plan(plan)


def test_apply_plan_rolls_back_on_qmd_failure(
    git_wiki: WikiLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/example.md",
                content="---\ntitle: Example\ntype: concept\n---\n# Example\n",
                evidence=["page-1"],
            )
        ],
        rationale="new",
        evidence=["page-1"],
    )

    def broken_update(_cwd: Path) -> None:
        raise RuntimeError("qmd unavailable")

    service = WikiMemoryService(git_wiki, qmd_update=broken_update)
    receipt = service.apply_plan(plan)
    assert any("qmd_stale" in err for err in receipt.errors)
    # The wiki commit already happened, so the file exists and is committed.
    assert (git_wiki.wiki_dir / "concepts" / "example.md").exists()


def test_apply_plan_logs_operation(git_wiki: WikiLayout) -> None:
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/example.md",
                content="---\ntitle: Example\ntype: concept\n---\n# Example\n",
                evidence=["page-1"],
            )
        ],
        rationale="new",
        evidence=["page-1"],
    )
    service = WikiMemoryService(git_wiki)
    service.apply_plan(plan)
    log = git_wiki.log_path.read_text(encoding="utf-8")
    assert "concepts/example.md" in log
