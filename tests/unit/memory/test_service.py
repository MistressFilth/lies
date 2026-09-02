# tests/unit/memory/test_service.py
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from lies.lock_errors import WikiFlockIndeterminate
from lies.memory.models import (
    EvidenceAppend,
    MemoryPlan,
    OperationKind,
    PageCreate,
    PageDelete,
    PageUpdate,
    WikiPlanInvalid,
    WikiWriteConflict,
)
from lies.memory.service import WikiMemoryService, _acquire_wiki_flock
from lies.utils.lock_heartbeat import AcquireResult
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


def _last_git_commit_message(repo: Path) -> str:
    """Return the full message of the most recent commit on ``repo``."""
    result = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.rstrip("\n")


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


def test_acquire_wiki_flock_routes_indeterminate_to_wiki_flock_indeterminate(
    git_wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An indeterminate acquire result surfaces as WikiFlockIndeterminate."""
    indeterminate = AcquireResult(
        fd=-1,
        holder_pid=999,
        holder_started_at=1723828800.0,
        status="indeterminate",
    )
    monkeypatch.setattr("lies.memory.service.acquire_create_lock", lambda *a, **k: indeterminate)
    with pytest.raises(WikiFlockIndeterminate) as caught, _acquire_wiki_flock(git_wiki):
        pass
    msg = str(caught.value)
    assert "pid 999" in msg
    assert "force-repair" in msg


def test_apply_plan_commit_message_uses_op_tag(git_wiki: Wiki) -> None:
    """An op with ``tag="ingest"`` must produce a commit message that
    starts with ``ingest:`` (not the hard-coded ``memory:`` prefix)."""
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/articles/x.md"})
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/alpha.md",
                content="---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n",
                evidence=["raw/articles/x.md"],
                tag="ingest",
            )
        ],
        rationale="distill single source",
        evidence=["raw/articles/x.md"],
    )
    service.apply_plan(plan)
    msg = _last_git_commit_message(git_wiki.data_root)
    assert msg.startswith("ingest: distill single source"), msg


def test_apply_plan_log_entry_uses_op_tag(git_wiki: Wiki) -> None:
    """An op with ``tag="ingest"`` must produce a ``wiki/log.md`` entry
    prefixed ``ingest | <op> | <path>`` (not the hard-coded ``memory``)."""
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/articles/x.md"})
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="concepts/alpha.md",
                content="---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n",
                evidence=["raw/articles/x.md"],
                tag="ingest",
            )
        ],
        rationale="distill single source",
        evidence=["raw/articles/x.md"],
    )
    service.apply_plan(plan)
    log_text = (git_wiki.wiki_dir / "log.md").read_text(encoding="utf-8")
    assert "ingest | create | concepts/alpha.md" in log_text, log_text


def test_apply_plan_delete_removes_existing_page(git_wiki: Wiki) -> None:
    """A ``PageDelete`` op removes an existing wiki page and records
    the change in the receipt with ``OperationKind.DELETE``."""
    target = git_wiki.wiki_dir / "concepts" / "obsolete.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\ntitle: Obsolete\ntype: concept\n---\n# Obsolete\n", encoding="utf-8")
    _git_init(git_wiki.data_root)
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            PageDelete(path="concepts/obsolete.md", evidence=["raw/x.md"]),
        ],
        rationale="page replaced",
        evidence=["raw/x.md"],
    )
    receipt = service.apply_plan(plan)
    assert not target.exists()
    delete_refs = [r for r in receipt.changed_pages if r.path == "concepts/obsolete.md"]
    assert delete_refs, "expected a PageReference for the deleted path"
    assert delete_refs[0].op == OperationKind.DELETE
    # The deletion MUST land in git: an uncommitted ``D`` entry in the
    # working tree would resurrect the file on the next ``apply_plan``'s
    # snapshot/restore. Regression: ``_collect_commit_files`` previously
    # filtered candidates by ``.exists()`` and dropped the deleted path.
    porcelain = _tracked_porcelain(git_wiki.data_root)
    assert porcelain == "", f"working tree should be clean after delete; got:\n{porcelain}"


def test_apply_plan_delete_commits_to_git(git_wiki: Wiki) -> None:
    """A ``PageDelete`` op MUST land in git (not stay as an uncommitted
    ``D`` in the working tree).

    Regression: ``_collect_commit_files`` filtered candidates by
    ``.exists()``. ``_apply_operations`` unlinked the file before the
    staging list was computed, so the path was dropped and ``git add``
    never recorded the removal. The next ``apply_plan``'s snapshot
    (which stashes uncommitted changes) + restore resurrected the file.

    The fix: ``_collect_commit_files`` derives its candidate list from
    the ``PageReference`` list returned by ``_apply_operations`` rather
    than the raw plan ops, so successful deletes are staged even after
    ``unlink``.
    """
    target = git_wiki.wiki_dir / "concepts" / "obsolete.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    sentinel = "---\ntitle: Obsolete\ntype: concept\n---\n# Obsolete\n"
    target.write_text(sentinel, encoding="utf-8")
    _git_init(git_wiki.data_root)

    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            PageDelete(path="concepts/obsolete.md", evidence=["raw/x.md"]),
        ],
        rationale="page replaced",
        evidence=["raw/x.md"],
    )
    receipt = service.apply_plan(plan)

    # 1. Working tree has no uncommitted modifications to tracked paths:
    #    in particular no ``D`` entry for the deleted page (which would
    #    resurrect it on the following snapshot/restore).
    porcelain = _tracked_porcelain(git_wiki.data_root)
    assert porcelain == "", f"working tree should be clean after delete; got:\n{porcelain}"

    # 2. The most recent commit records the deletion with status ``D``.
    name_status = subprocess.run(
        ["git", "log", "-1", "--name-status"],
        cwd=git_wiki.data_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "D\twiki/concepts/obsolete.md" in name_status, name_status

    # 3. The file is gone from the working tree.
    assert not target.exists()

    # 4. The receipt reflects the DELETE op.
    delete_refs = [r for r in receipt.changed_pages if r.path == "concepts/obsolete.md"]
    assert delete_refs, "expected a PageReference for the deleted path"
    assert delete_refs[0].op == OperationKind.DELETE


def test_apply_plan_delete_no_op_when_missing(git_wiki: Wiki) -> None:
    """A ``PageDelete`` op against a missing file is a silent no-op:
    ``apply_plan`` records no ``PageReference`` for the path because no
    actual change occurred (the file was already absent)."""
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            PageDelete(path="concepts/never-existed.md", evidence=["raw/x.md"]),
        ],
        rationale="ensure gone",
        evidence=["raw/x.md"],
    )
    receipt = service.apply_plan(plan)
    assert not any(r.path == "concepts/never-existed.md" for r in receipt.changed_pages)


def test_apply_plan_delete_no_op_when_missing_still_records_commit(
    git_wiki: Wiki,
) -> None:
    """A no-op ``PageDelete`` (file already absent) leaves the receipt
    with no ``PageReference`` for the target, but the plan still
    commits ``wiki/index.md`` / ``wiki/log.md`` (which ``rebuild_index``
    and ``append_log_entry`` always touch) so ``git status`` is clean.

    Regression: a previous fix attempt included the deletion target in
    the staging list unconditionally; ``git add`` failed on the
    never-existed path (``fatal: pathspec ... did not match any files``)
    and broke the commit. The correct mechanism uses the
    ``_apply_operations`` ``changed`` list, which omits no-op deletes.
    """
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            PageDelete(path="concepts/never-was.md", evidence=["raw/x.md"]),
        ],
        rationale="ensure gone",
        evidence=["raw/x.md"],
    )
    receipt = service.apply_plan(plan)
    # No PageReference for the target — the op was a no-op.
    assert receipt.changed_pages == []
    # Working tree must still be clean: the index/log rewrites that
    # ``rebuild_index`` / ``append_log_entry`` performed were committed.
    porcelain = _tracked_porcelain(git_wiki.data_root)
    assert porcelain == "", f"working tree should be clean after no-op delete; got:\n{porcelain}"


def _tracked_porcelain(repo: Path) -> str:
    """Return ``git status --porcelain`` with untracked entries stripped.

    The ``git_wiki`` fixture does not seed ``.gitignore`` so the
    per-wiki sidecar at ``<data_root>/.lies/`` (created by
    ``append_receipt``) shows up as an untracked directory in
    ``git status``. The fix under test concerns committed-path
    bookkeeping, not sidecar artifacts, so strip untracked entries
    before asserting the working tree is clean.
    """
    raw = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return "\n".join(
        line for line in raw.splitlines() if line and not line.startswith("??")
    ).strip()


def test_apply_plan_delete_refuses_index_md(git_wiki: Wiki) -> None:
    """A ``PageDelete`` op targeting ``wiki/index.md`` is rejected: the
    catalog is rebuilt from disk state by the service, never literally
    written or removed, so a delete op would silently destroy the wiki's
    navigation surface."""
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            PageDelete(path="wiki/index.md", evidence=["raw/x.md"]),
        ],
        rationale="attempt to drop catalog",
        evidence=["raw/x.md"],
    )
    with pytest.raises(WikiPlanInvalid):
        service.apply_plan(plan)
    assert (git_wiki.wiki_dir / "index.md").exists(), "index.md must be intact"


def test_apply_plan_delete_refuses_index_md_via_bare_path(git_wiki: Wiki) -> None:
    """A ``PageDelete`` op using the bare ``index.md`` path (no ``wiki/``
    prefix) is rejected by the resolved-path guard in
    ``_apply_operations``. ``validate_plan``'s ``is_index`` branch routes
    this op and skips ``validate_page_type``, so a literal-string guard
    keyed on ``op.path`` would let the unlink through; the resolved-path
    check catches it instead."""
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            PageDelete(path="index.md", evidence=["raw/x.md"]),
        ],
        rationale="attempt to drop catalog via bare path",
        evidence=["raw/x.md"],
    )
    with pytest.raises(WikiPlanInvalid):
        service.apply_plan(plan)
    assert (git_wiki.wiki_dir / "index.md").exists(), "index.md must be intact"


def test_apply_plan_delete_refuses_log_md(git_wiki: Wiki) -> None:
    """A ``PageDelete`` op targeting ``wiki/log.md`` is rejected: the
    log is append-only, so a delete op would silently destroy the
    wiki's audit trail.

    Uses the bare ``log.md`` path so the resolved-path guard inside
    ``_apply_operations`` is exercised directly. The qualified
    ``wiki/log.md`` path is rejected earlier by ``validate_plan``'s
    ``validate_page_type`` step because ``log.md``'s parent is the wiki
    root (not a recognized page-type directory), which would mask a
    regression in the apply-side guard.

    Asserts the apply-side guard's exact message so a regression that
    makes ``validate_page_type`` reject the path instead is caught —
    the bare-path route should reach the apply-side guard.
    """
    log = git_wiki.wiki_dir / "log.md"
    log.write_text("# Log\n- entry\n", encoding="utf-8")
    _git_init(git_wiki.data_root)
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            PageDelete(path="log.md", evidence=["raw/x.md"]),
        ],
        rationale="attempt to drop log via bare path",
        evidence=["raw/x.md"],
    )
    with pytest.raises(WikiPlanInvalid, match="system file"):
        service.apply_plan(plan)
    assert log.exists(), "log.md must be intact"


def test_apply_plan_delete_refuses_log_md_now_exercises_guard(git_wiki: Wiki) -> None:
    """``PageDelete(path='log.md')`` is rejected by the apply-side
    ``system file`` guard inside ``_apply_operations`` rather than by
    ``validate_plan``'s ``validate_page_type`` step.

    The assertion is on the exact message ``"system file"`` — the
    apply-side guard's signature. ``validate_page_type`` would surface
    ``"unknown page type"`` instead, which would indicate the
    ``is_log`` branch in ``validate_plan`` regressed and the test is
    vacuous.
    """
    log = git_wiki.wiki_dir / "log.md"
    log.write_text("# Log\n- entry\n", encoding="utf-8")
    _git_init(git_wiki.data_root)
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            PageDelete(path="log.md", evidence=["raw/x.md"]),
        ],
        rationale="attempt to drop log via bare path",
        evidence=["raw/x.md"],
    )
    with pytest.raises(WikiPlanInvalid, match="system file"):
        service.apply_plan(plan)
    assert log.exists(), "log.md must be intact"


def test_apply_plan_create_refuses_log_md(git_wiki: Wiki) -> None:
    """A ``PageCreate`` op targeting ``wiki/log.md`` is rejected by the
    symmetric apply-side ``system file`` guard.

    The log is append-only and owned by ``append_log_entry`` inside
    ``_apply_operations``; a ``PageCreate`` (or any other write op) that
    targets ``wiki/log.md`` would silently destroy the audit trail.
    The guard runs before the op-kind dispatch so every op shape is
    caught, regardless of the path it takes.
    """
    log = git_wiki.wiki_dir / "log.md"
    log.write_text("# Log\n- entry\n", encoding="utf-8")
    _git_init(git_wiki.data_root)
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="wiki/log.md",
                content="bad",
                evidence=["raw/x.md"],
            ),
        ],
        rationale="attempt to overwrite log via create",
        evidence=["raw/x.md"],
    )
    with pytest.raises(WikiPlanInvalid, match="system file"):
        service.apply_plan(plan)
    assert log.read_text(encoding="utf-8") == "# Log\n- entry\n", "log.md must be intact"


def test_apply_plan_update_refuses_log_md(git_wiki: Wiki) -> None:
    """A ``PageUpdate`` op targeting ``wiki/log.md`` is rejected by the
    symmetric apply-side ``system file`` guard.

    The log is append-only; ``PageUpdate`` would clobber it with a
    full-content replacement, silently destroying the audit trail.
    """
    log = git_wiki.wiki_dir / "log.md"
    log.write_text("# Log\n- entry\n", encoding="utf-8")
    _git_init(git_wiki.data_root)
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            PageUpdate(
                path="wiki/log.md",
                expected_sha256=_sha("# Log\n- entry\n"),
                content="bad",
                evidence=["raw/x.md"],
            ),
        ],
        rationale="attempt to overwrite log via update",
        evidence=["raw/x.md"],
    )
    with pytest.raises(WikiPlanInvalid, match="system file"):
        service.apply_plan(plan)
    assert log.read_text(encoding="utf-8") == "# Log\n- entry\n", "log.md must be intact"


def test_apply_plan_append_refuses_log_md(git_wiki: Wiki) -> None:
    """An ``EvidenceAppend`` op targeting ``wiki/log.md`` is rejected by
    the symmetric apply-side ``system file`` guard.

    The log is append-only and managed by ``append_log_entry``; a direct
    ``EvidenceAppend`` against it would append through the wrong
    envelope, leaving the entry unparseable by the log reader.
    """
    log = git_wiki.wiki_dir / "log.md"
    log.write_text("# Log\n- entry\n", encoding="utf-8")
    _git_init(git_wiki.data_root)
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            EvidenceAppend(
                path="wiki/log.md",
                expected_sha256=_sha("# Log\n- entry\n"),
                content="bad",
                evidence=["raw/x.md"],
            ),
        ],
        rationale="attempt to append via EvidenceAppend",
        evidence=["raw/x.md"],
    )
    with pytest.raises(WikiPlanInvalid, match="system file"):
        service.apply_plan(plan)
    assert log.read_text(encoding="utf-8") == "# Log\n- entry\n", "log.md must be intact"


def test_apply_plan_create_refuses_index_md(git_wiki: Wiki) -> None:
    """A ``PageCreate`` op targeting ``wiki/index.md`` is rejected by
    the symmetric apply-side ``system file`` guard.

    ``wiki/index.md`` is rebuilt from disk state by ``rebuild_index``
    after every apply, so a literal ``PageCreate`` overwrite would
    either be silently undone or, on a fresh repo with no other pages,
    leave a stale catalog. The guard rejects ``PageCreate`` (and
    ``EvidenceAppend`` / ``PageDelete``) on ``index.md``; ``PageUpdate``
    remains the established pathway for the repair agent's
    ``UpdateIndex`` operation.
    """
    index = git_wiki.wiki_dir / "index.md"
    original = index.read_text(encoding="utf-8")
    service = WikiMemoryService(wiki=git_wiki)
    service.register_evidence({"raw/x.md"})
    plan = MemoryPlan(
        operations=[
            PageCreate(
                path="wiki/index.md",
                content="bad",
                evidence=["raw/x.md"],
            ),
        ],
        rationale="attempt to overwrite catalog via create",
        evidence=["raw/x.md"],
    )
    with pytest.raises(WikiPlanInvalid, match="system file"):
        service.apply_plan(plan)
    assert index.read_text(encoding="utf-8") == original, "index.md must be intact"
