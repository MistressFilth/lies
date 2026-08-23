# tests/unit/memory/test_service.py
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from lies.memory.models import (
    MemoryPlan,
    PageCreate,
    PageUpdate,
    WikiPlanInvalid,
    WikiWriteConflict,
)
from lies.memory.service import WikiMemoryService, _acquire_wiki_flock
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki


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
def git_wiki(tmp_path: Path) -> Wiki:
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    wiki = make_wiki(name="service", data_root=root)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    (wiki.wiki_dir / "concepts").mkdir(parents=True)
    (wiki.wiki_dir / "index.md").write_text("# Index\n", encoding="utf-8")
    _git_init(root)
    return wiki


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_apply_plan_creates_page(git_wiki: Wiki) -> None:
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
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


def test_apply_plan_updates_page_with_matching_hash(git_wiki: Wiki) -> None:
    path = git_wiki.wiki_dir / "concepts" / "x.md"
    body = "---\ntitle: X\ntype: concept\n---\n# X\n"
    path.write_text(body, encoding="utf-8")
    _git_init(git_wiki.data_root)
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
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    receipt = service.apply_plan(plan)
    assert receipt.changed_pages
    assert "New section" in path.read_text(encoding="utf-8")


def test_apply_plan_rejects_hash_mismatch(git_wiki: Wiki) -> None:
    path = git_wiki.wiki_dir / "concepts" / "x.md"
    path.write_text("---\ntitle: X\ntype: concept\n---\n# X\n", encoding="utf-8")
    _git_init(git_wiki.data_root)
    plan = MemoryPlan(
        operations=[
            PageUpdate(
                path="concepts/x.md",
                expected_sha256="0" * 64,
                content="---\ntitle: X\ntype: concept\n---\n# X changed\n",
                evidence=["page-1"],
            )
        ],
        rationale="bad hash",
        evidence=["page-1"],
    )
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    with pytest.raises(WikiWriteConflict):
        service.apply_plan(plan)


def test_apply_plan_rejects_path_escape(git_wiki: Wiki) -> None:
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
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    with pytest.raises(WikiPlanInvalid):
        service.apply_plan(plan)


def test_apply_plan_rolls_back_on_qmd_failure(
    git_wiki: Wiki, monkeypatch: pytest.MonkeyPatch
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

    service = WikiMemoryService(wiki=git_wiki, qmd_update=broken_update)
    service.register_evidence({"page-1"})
    receipt = service.apply_plan(plan)
    assert any("qmd_stale" in err for err in receipt.errors)
    # The wiki commit already happened, so the file exists and is committed.
    assert (git_wiki.wiki_dir / "concepts" / "example.md").exists()


def test_apply_plan_logs_operation(git_wiki: Wiki) -> None:
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
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    service.apply_plan(plan)
    log = (git_wiki.wiki_dir / "log.md").read_text(encoding="utf-8")
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
    git_wiki: Wiki,
) -> None:
    """Atomic commit must include the new page, index.md, and log.md.

    Regression: ``atomic_commit`` defaults to ``git add -u`` which only
    stages modifications to already-tracked files. Untracked files
    (newly created pages, a freshly written ``wiki/log.md``) would be
    silently skipped. The service must enumerate the files explicitly.
    """
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
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
    changed = set(_commit_changed_files(git_wiki.data_root))
    assert "wiki/concepts/example.md" in changed
    assert "wiki/index.md" in changed
    assert "wiki/log.md" in changed


def test_apply_plan_rolls_back_writes_on_apply_failure(
    git_wiki: Wiki, monkeypatch: pytest.MonkeyPatch
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
        (git_wiki.wiki_dir / "log.md").parent.mkdir(parents=True, exist_ok=True)
        (git_wiki.wiki_dir / "log.md").write_text(
            "## partial log entry from the simulated failure\n",
            encoding="utf-8",
        )
        raise WikiWriteConflict("simulated hash mismatch for concepts/example.md")

    monkeypatch.setattr(_Svc, "_apply_operations", fail_after_partial_writes)

    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    with pytest.raises(WikiWriteConflict):
        service.apply_plan(plan)

    # The page written by the simulated partial apply must be gone.
    assert not page_path.exists()
    # The log line written by the simulated partial apply must be gone.
    log = (
        (git_wiki.wiki_dir / "log.md").read_text(encoding="utf-8")
        if (git_wiki.wiki_dir / "log.md").exists()
        else ""
    )
    assert "rollback-on-hash-mismatch" not in log
    assert "memory" not in log
    # No new commit should have been created on the failure path.
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_wiki.data_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_sha  # the repo still has its initial commit, nothing new


def test_hash_page_empty_file_hashes_empty_string(
    git_wiki: Wiki,
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

    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    assert service.hash_page("concepts/empty.md") == hashlib.sha256(b"").hexdigest()


def test_hash_page_missing_file_returns_empty_sentinel(
    git_wiki: Wiki,
) -> None:
    """A missing file still returns ``""`` (the missing-page sentinel)."""
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    assert service.hash_page("concepts/does-not-exist.md") == ""


def test_apply_plan_restores_dirty_tree_when_commit_fails(
    git_wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirty = git_wiki.data_root / "notes.txt"
    dirty.write_text("keep me", encoding="utf-8")
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/new.md",
                content="---\ntitle: New\ntype: concept\n---\n# New\n",
                evidence=["page-1"],
            )
        ],
        rationale="commit failure",
        evidence=["page-1"],
    )

    def fail_commit(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("commit failed")

    monkeypatch.setattr("lies.memory.service.atomic_commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        service.apply_plan(plan)
    assert dirty.read_text(encoding="utf-8") == "keep me"
    assert not (git_wiki.wiki_dir / "concepts" / "new.md").exists()


def test_apply_plan_returns_empty_receipt_when_atomic_commit_is_noop(
    git_wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``atomic_commit`` returning ``None`` (no-op) must short-circuit to
    :meth:`WikiMemoryService._empty_receipt`.

    Regression: ``apply_plan`` discarded the return value and unconditionally
    refreshed qmd + returned a ``MemoryReceipt`` with ``changed_pages`` from
    the in-memory ``_apply_operations`` list. When ``atomic_commit`` detected
    the staged diff was empty (e.g. an idempotent plan wrote byte-identical
    content), the receipt falsely claimed those changes were applied at the
    git level. Capture ``commit_sha``; on ``None`` restore the working tree
    and return the empty receipt.
    """
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/example.md",
                content="---\ntitle: Example\ntype: concept\n---\n# Example\n",
                evidence=["page-1"],
            )
        ],
        rationale="idempotent re-write",
        evidence=["page-1"],
    )

    noop_count = {"calls": 0}

    def fake_atomic_commit(*_args: object, **_kwargs: object) -> str | None:
        noop_count["calls"] += 1
        # atomic_commit returns None on a no-op; the service must route
        # through _empty_receipt() instead of claiming changes landed.
        return

    monkeypatch.setattr("lies.memory.service.atomic_commit", fake_atomic_commit)
    receipt = service.apply_plan(plan)
    assert noop_count["calls"] == 1
    # The receipt must mirror _empty_receipt() — no claimed changes at git
    # level, no qmd errors, no deferrals, no fallback.
    assert receipt.changed_pages == []
    assert receipt.deferred == []
    assert receipt.fallback_used is False
    assert receipt.fallback_reason == ""
    assert receipt.errors == []
    # And no qmd refresh should have run (receipt.errors is empty AND the
    # on-disk qmd side-effects of a real commit are absent).
    assert not (git_wiki.wiki_dir / "concepts" / "example.md").exists()


def test_validate_plan_rejects_create_collision(git_wiki: Wiki) -> None:
    page = git_wiki.wiki_dir / "concepts" / "existing.md"
    page.write_text("---\ntitle: Existing\ntype: concept\n---\n", encoding="utf-8")
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    service.register_evidence({"page-1"})
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/existing.md",
                content="---\ntitle: Replacement\ntype: concept\n---\n",
                evidence=["page-1"],
            )
        ],
        rationale="replace",
        evidence=["page-1"],
    )
    with pytest.raises(WikiPlanInvalid, match="page already exists; use UPDATE or APPEND"):
        service.validate_plan(plan)


def test_validate_plan_rejects_frontmatter_type_mismatch(git_wiki: Wiki) -> None:
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    service.register_evidence({"page-1"})
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/wrong.md",
                content="---\ntitle: Wrong\ntype: entity\n---\n",
                evidence=["page-1"],
            )
        ],
        rationale="wrong type",
        evidence=["page-1"],
    )
    with pytest.raises(WikiPlanInvalid, match="does not match"):
        service.validate_plan(plan)


def test_search_filters_single_collection_and_registers_evidence(
    git_wiki: Wiki, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = git_wiki.wiki_dir / "concepts" / "x.md"
    page.write_text("---\ntitle: X\ntype: concept\n---\n# X\nEvidence.\n", encoding="utf-8")
    (git_wiki.wiki_dir / "index.md").write_text("- [X](concepts/x.md)\n", encoding="utf-8")
    monkeypatch.setattr(
        "lies.qmd.cli.qmd_query",
        lambda *a, **kw: [{"path": "concepts/x.md", "score": 1.0}],
    )
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"page-1"})
    result = service.search("X", collection_ids=[git_wiki.name])
    assert result.pages
    assert service.known_evidence
    filtered = service.search("X", collection_ids=["other"])
    assert filtered.pages == []


def test_service_locks_are_per_instance(git_wiki: Wiki) -> None:
    first = WikiMemoryService(wiki=git_wiki)
    second = WikiMemoryService(wiki=git_wiki)
    assert first._lock is not second._lock


def test_register_collection_is_idempotent(tmp_path) -> None:
    from lies.memory.models import WikiCollectionRef
    from lies.memory.service import WikiMemoryService

    wiki = make_wiki(name="register", data_root=tmp_path)
    svc = WikiMemoryService(wiki=wiki)
    ref = WikiCollectionRef(
        collection_id="htmx",
        root=PurePosixPath(str(tmp_path / "raw" / "htmx")),
        qmd_collection="htmx",
        schema_path=PurePosixPath(str(wiki.schema_path)),
    )
    svc.register_collection(ref)
    svc.register_collection(ref)
    assert svc.is_registered("htmx")
    assert len(svc.registered_collections()) == 1


def test_is_registered_false_for_unknown() -> None:
    import pathlib

    from lies.memory.service import WikiMemoryService

    wiki = make_wiki(name="unknown", data_root=pathlib.Path("/tmp/lies-svc-test"))
    svc = WikiMemoryService(wiki=wiki)
    assert not svc.is_registered("nope")


def test_init_hydrates_registered_from_disk(tmp_path) -> None:
    from lies.collections.registry import Registry
    from lies.memory.models import WikiCollectionRef
    from lies.memory.service import WikiMemoryService

    wiki = make_wiki(name="hyd", data_root=tmp_path / "wiki")
    wiki.registry_path.parent.mkdir(parents=True, exist_ok=True)
    Registry.save(
        wiki,
        Registry(
            collections={
                "htmx": WikiCollectionRef(
                    collection_id="htmx",
                    root=PurePosixPath("/raw/htmx"),
                    qmd_collection="htmx",
                    schema_path=PurePosixPath(str(wiki.schema_path)),
                )
            }
        ),
    )
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "htmx.yaml").write_text("name: htmx\n", encoding="utf-8")
    svc = WikiMemoryService(wiki=wiki)
    assert svc.is_registered("htmx")
    assert len(svc.registered_collections()) == 1


def test_init_filters_stale_entries(tmp_path) -> None:
    from pathlib import PurePosixPath

    from lies.collections.registry import Registry
    from lies.memory.models import WikiCollectionRef
    from lies.memory.service import WikiMemoryService

    wiki = make_wiki(name="stale", data_root=tmp_path / "wiki")
    wiki.registry_path.parent.mkdir(parents=True, exist_ok=True)
    Registry.save(
        wiki,
        Registry(
            collections={
                "alive": WikiCollectionRef(
                    collection_id="alive",
                    root=PurePosixPath("/raw/alive"),
                    qmd_collection="alive",
                    schema_path=PurePosixPath(str(wiki.schema_path)),
                ),
                "ghost": WikiCollectionRef(
                    collection_id="ghost",
                    root=PurePosixPath("/raw/ghost"),
                    qmd_collection="ghost",
                    schema_path=PurePosixPath(str(wiki.schema_path)),
                ),
            }
        ),
    )
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "alive.yaml").write_text("name: alive\n", encoding="utf-8")
    svc = WikiMemoryService(wiki=wiki)
    assert svc.is_registered("alive")
    assert not svc.is_registered("ghost")
    assert {r.collection_id for r in svc.registered_collections()} == {"alive"}


def test_register_collection_persists_to_disk(tmp_path) -> None:
    from pathlib import PurePosixPath

    from lies.memory.models import WikiCollectionRef
    from lies.memory.service import WikiMemoryService

    wiki = make_wiki(name="persist", data_root=tmp_path / "wiki")
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "c1.yaml").write_text("name: c1\n", encoding="utf-8")
    svc = WikiMemoryService(wiki=wiki)
    ref = WikiCollectionRef(
        collection_id="c1",
        root=PurePosixPath("/raw/c1"),
        qmd_collection="c1",
        schema_path=PurePosixPath(str(wiki.schema_path)),
    )
    svc.register_collection(ref)
    # Fresh service instance sees the registration.
    svc2 = WikiMemoryService(wiki=wiki)
    assert svc2.is_registered("c1")


def test_register_collection_idempotent_on_disk(tmp_path) -> None:
    from pathlib import PurePosixPath

    from lies.collections.registry import Registry
    from lies.memory.models import WikiCollectionRef
    from lies.memory.service import WikiMemoryService

    wiki = make_wiki(name="idem", data_root=tmp_path / "wiki")
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "c1.yaml").write_text("name: c1\n", encoding="utf-8")
    svc = WikiMemoryService(wiki=wiki)
    ref = WikiCollectionRef(
        collection_id="c1",
        root=PurePosixPath("/raw/c1"),
        qmd_collection="c1",
        schema_path=PurePosixPath(str(wiki.schema_path)),
    )
    svc.register_collection(ref)
    svc.register_collection(ref)
    on_disk = Registry.load(wiki)
    assert len(on_disk.collections) == 1


def test_register_collection_preserves_other_entries_on_disk(tmp_path) -> None:
    """Read-merge-write union: an in-memory register must not clobber other writers."""
    from pathlib import PurePosixPath

    from lies.collections.registry import Registry
    from lies.memory.models import WikiCollectionRef
    from lies.memory.service import WikiMemoryService

    wiki = make_wiki(name="union", data_root=tmp_path / "wiki")
    wiki.registry_path.parent.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "other.yaml").write_text("name: other\n", encoding="utf-8")
    (wiki.collections_dir / "new.yaml").write_text("name: new\n", encoding="utf-8")
    Registry.save(
        wiki,
        Registry(
            collections={
                "other": WikiCollectionRef(
                    collection_id="other",
                    root=PurePosixPath("/raw/other"),
                    qmd_collection="other",
                    schema_path=PurePosixPath(str(wiki.schema_path)),
                )
            }
        ),
    )
    svc = WikiMemoryService(wiki=wiki)  # loads "other"
    svc.register_collection(
        WikiCollectionRef(
            collection_id="new",
            root=PurePosixPath("/raw/new"),
            qmd_collection="new",
            schema_path=PurePosixPath(str(wiki.schema_path)),
        )
    )
    on_disk = Registry.load(wiki)
    assert set(on_disk.collections.keys()) == {"other", "new"}


def test_acquire_wiki_flock_writes_heartbeat_on_acquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_wiki: Wiki,
) -> None:
    """Acquiring the memory flock must write pid + heartbeat alongside
    the lock files; on release all three are unlinked."""
    wiki = git_wiki
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    monkeypatch.setattr("lies.utils.exclusive.os.kill", lambda pid, sig: None)

    with _acquire_wiki_flock(wiki):
        assert wiki.memory_create_lock_path.exists()
        assert wiki.memory_pid_path.exists()
        assert wiki.memory_heartbeat_path.exists()
        hb = json.loads(wiki.memory_heartbeat_path.read_text())
        assert hb["pid"] == os.getpid()

    assert not wiki.memory_create_lock_path.exists()
    assert not wiki.memory_pid_path.exists()
    assert not wiki.memory_heartbeat_path.exists()
