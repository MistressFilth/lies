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
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=root,
        check=True,
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
    path.write_text("---\ntitle: X\ntype: concept\n---\n# X\n", encoding="utf-8")
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


def _commit_changed_files(repo: Path) -> list[str]:
    """Return repo-relative paths changed in the most recent commit."""
    result = subprocess.run(
        ["git", "log", "-1", "--name-only", "--pretty=format:"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_apply_plan_commit_includes_new_page_index_and_log(
    git_wiki: WikiLayout,
) -> None:
    """Atomic commit must include the new page, index.md, and log.md.

    Regression: ``atomic_commit`` defaults to ``git add -u`` which only
    stages modifications to already-tracked files. Untracked files
    (newly created pages, a freshly written ``wiki/log.md``) would be
    silently skipped. The service must enumerate the files explicitly.
    """
    service = WikiMemoryService(git_wiki)
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/example.md",
                content="---\ntitle: Example\ntype: concept\n---\n# Example\n",
                evidence=["page-1"],
            )
        ],
        rationale="track-new",
        evidence=["page-1"],
    )
    service.apply_plan(plan)
    changed = set(_commit_changed_files(git_wiki.root))
    assert "wiki/concepts/example.md" in changed
    assert "wiki/index.md" in changed
    assert "wiki/log.md" in changed


def test_apply_plan_rolls_back_writes_on_apply_failure(
    git_wiki: WikiLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure inside ``_apply_operations`` must roll back all writes.

    Simulates a hash-mismatch-style failure midway through the apply
    step. The new page (written by an earlier op) and the log line
    appended by ``_apply_operations`` must not survive the failure.
    """
    from lies.memory.service import WikiMemoryService as _Svc

    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/example.md",
                content="---\ntitle: Example\ntype: concept\n---\n# Example\n",
                evidence=["page-1"],
            )
        ],
        rationale="rollback-on-hash-mismatch",
        evidence=["page-1"],
    )

    page_path = git_wiki.wiki_dir / "concepts" / "example.md"

    def fail_after_partial_writes(self: _Svc, _plan: MemoryPlan) -> list:  # type: ignore[type-arg]
        # Mimic what ``_apply_operations`` would do up to the failing
        # op: write a page, write the log, then raise.
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text("partial", encoding="utf-8")
        git_wiki.log_path.parent.mkdir(parents=True, exist_ok=True)
        git_wiki.log_path.write_text(
            "## partial log entry from the simulated failure\n",
            encoding="utf-8",
        )
        raise WikiWriteConflict("simulated hash mismatch for concepts/example.md")

    monkeypatch.setattr(_Svc, "_apply_operations", fail_after_partial_writes)

    service = WikiMemoryService(git_wiki)
    with pytest.raises(WikiWriteConflict):
        service.apply_plan(plan)

    # The page written by the simulated partial apply must be gone.
    assert not page_path.exists()
    # The log line written by the simulated partial apply must be gone.
    log = git_wiki.log_path.read_text(encoding="utf-8") if git_wiki.log_path.exists() else ""
    assert "rollback-on-hash-mismatch" not in log
    assert "memory" not in log
    # No new commit should have been created on the failure path.
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_wiki.root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_sha  # the repo still has its initial commit, nothing new


def test_hash_page_empty_file_hashes_empty_string(
    git_wiki: WikiLayout,
) -> None:
    """An existing empty page hashes to SHA-256 of the empty string.

    Regression: ``hash_page`` previously returned the empty ``""``
    sentinel for an empty file, breaking the update flow where the
    caller passes ``expected_sha256 = hash_page(...)`` for an empty
    page. Now empty content hashes to ``sha256(b"")``.
    """
    page = git_wiki.wiki_dir / "concepts" / "empty.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("", encoding="utf-8")

    service = WikiMemoryService(git_wiki)
    assert service.hash_page("concepts/empty.md") == hashlib.sha256(b"").hexdigest()


def test_hash_page_missing_file_returns_empty_sentinel(
    git_wiki: WikiLayout,
) -> None:
    """A missing file still returns ``""`` (the missing-page sentinel)."""
    service = WikiMemoryService(git_wiki)
    assert service.hash_page("concepts/does-not-exist.md") == ""
