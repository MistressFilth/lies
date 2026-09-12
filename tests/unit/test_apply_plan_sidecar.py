"""apply_plan integration with the JSONL receipt sidecar."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lies.memory.models import MemoryPlan, PageCreate
from lies.memory.service import WikiMemoryService
from lies.wiki.wiki import Wiki

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _mock_external_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub git + qmd so the unit suite never shells out for either.

    ``WikiMemoryService.apply_plan`` calls ``atomic_commit`` (5 git
    subprocess calls per commit) and then ``qmd_update`` (another
    subprocess). It also runs the snapshot envelope
    (``_snapshot_working_tree`` → ``git stash push``,
    ``_discard_snapshot`` → ``git stash drop``,
    ``_restore_working_tree`` → ``git stash pop``) — three more git
    subprocess calls per apply. The sidecar-on-disk assertions below
    are the contract under test; the git history, the qmd index, and
    the rollback envelope are not. (No test in this file asserts the
    rollback contract; see ``tests/unit/memory/test_service.py`` for
    that.)

    Each ``atomic_commit`` call yields a distinct synthetic SHA so the
    SHA-dedup invariant in the sidecar (the test that calls
    ``apply_plan`` twice and expects two rows) holds without a real
    git history.
    """
    counter = {"n": 0}

    def fake_atomic_commit(repo, message, files=None):  # type: ignore[no-untyped-def]
        counter["n"] += 1
        # 40 hex chars; the trailing byte encodes the call index so
        # two distinct calls return two distinct SHAs.
        return f"deadbeef{counter['n']:034x}"

    monkeypatch.setattr("lies.memory.service.atomic_commit", fake_atomic_commit)
    monkeypatch.setattr("lies.memory.service.qmd_update", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "lies.memory.service.WikiMemoryService._snapshot_working_tree",
        lambda _self, _repo: "fake-stash-ref",
    )
    monkeypatch.setattr(
        "lies.memory.service.WikiMemoryService._restore_working_tree",
        lambda _self, _repo, _ref: None,
    )
    monkeypatch.setattr(
        "lies.memory.service.WikiMemoryService._discard_snapshot",
        lambda _self, _repo, _ref: None,
    )


def _git_init(root: Path) -> None:
    """Initialise the test wiki's git repo with a ``.gitignore`` covering ``.lies/``.

    The sidecar lives at ``<data_root>/.lies/memory_plans.jsonl``. Without
    ``.lies/`` in the wiki's ``.gitignore``, ``git stash push
    --include-untracked`` (used by ``apply_plan`` to snapshot the working
    tree) stashes the untracked sidecar and ``git stash drop`` discards
    it on success — silently losing prior sidecar lines. Ignoring
    ``.lies/`` keeps the stash snapshot inert against the sidecar.

    ``apply_plan`` also calls real ``git stash push`` / ``git stash pop``
    for the pre-commit snapshot, so a real git repo is required even
    though the autouse ``_mock_external_services`` fixture stubs the
    commit-and-qmd path. ``atomic_commit`` and ``qmd_update`` are the
    two callers we skip; the snapshot envelope stays live.
    """
    (root / ".gitignore").write_text(".lies/\n", encoding="utf-8")
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
def wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """A working wiki rooted at ``tmp_path/wiki``.

    No real ``git init`` runs — the autouse ``_mock_external_services``
    fixture stubs ``atomic_commit``, ``_snapshot_working_tree``, and
    ``_discard_snapshot``, so the apply path never shells out. Only the
    ``.gitignore`` is written because ``apply_plan`` reads it to decide
    which paths to skip during the pre-commit snapshot envelope.
    """
    xdg_root = tmp_path / "xdg"
    for sub in ("data", "config", "cache", "state", "runtime"):
        (xdg_root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_root / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_root / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_root / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg_root / "runtime"))

    root = tmp_path / "wiki"
    (root / "wiki").mkdir(parents=True)
    (root / "raw").mkdir(parents=True)
    w = Wiki(
        name="t",
        data_root=root,
        config_root=xdg_root / "config" / "lies" / "t",
        cache_root=xdg_root / "cache" / "lies" / "t",
        state_root=xdg_root / "state" / "lies" / "t",
        runtime_root=xdg_root / "runtime" / "lies" / "t",
    )
    w.config_root.mkdir(parents=True, exist_ok=True)
    (w.wiki_dir / "entities").mkdir(parents=True)
    (w.wiki_dir / "concepts").mkdir(parents=True)
    (w.wiki_dir / "index.md").write_text("# Index\n", encoding="utf-8")
    (root / ".gitignore").write_text(".lies/\n", encoding="utf-8")
    return w


_SAMPLE_CONTENT = "---\ntitle: Postgres\ntype: entity\n---\n# Postgres\n"


def _make_plan() -> MemoryPlan:
    return MemoryPlan(
        rationale="create entity page",
        operations=[
            PageCreate(
                path="entities/postgres.md",
                content=_SAMPLE_CONTENT,
                evidence=["ref-1"],
            )
        ],
        evidence=["ref-1"],
    )


def test_apply_plan_writes_sidecar_line_on_commit(wiki: Wiki) -> None:
    svc = WikiMemoryService(wiki=wiki)
    svc.register_evidence({"ref-1"})
    plan = _make_plan()
    receipt = svc.apply_plan(plan)
    sidecar = wiki.data_root / ".lies" / "memory_plans.jsonl"
    assert sidecar.exists()
    rows = [json.loads(ln) for ln in sidecar.read_text().splitlines() if ln]
    assert len(rows) == 1
    assert rows[0]["rationale"] == "create entity page"
    assert rows[0]["commit_sha"]  # truthy
    assert receipt.changed_pages, "wiki really did change"


def test_apply_plan_appends_after_qmd_refresh(wiki: Wiki) -> None:
    """Sidecar append ordering: commit -> sidecar -> qmd.

    Patches qmd refresh to fail and verifies the sidecar line still lands
    AND the receipt carries the qmd error (not sidecar).
    """
    svc = WikiMemoryService(wiki=wiki)
    svc.register_evidence({"ref-1"})
    plan = _make_plan()
    with patch.object(svc, "_refresh_qmd", return_value=(False, "qmd down")):
        receipt = svc.apply_plan(plan)
    assert any("qmd" in e for e in receipt.errors)
    sidecar = wiki.data_root / ".lies" / "memory_plans.jsonl"
    assert sidecar.exists()
    rows = [json.loads(ln) for ln in sidecar.read_text().splitlines() if ln]
    assert len(rows) == 1
    assert receipt.changed_pages, "wiki really did change"


def test_apply_plan_surfaces_sidecar_failure_in_receipt(wiki: Wiki) -> None:
    """Sidecar append failure -> receipt errors=[sidecar_append_failed: ...]."""
    svc = WikiMemoryService(wiki=wiki)
    svc.register_evidence({"ref-1"})
    plan = _make_plan()
    from lies.memory import sidecar as sidecar_mod

    def boom(*_a: object, **_kw: object) -> None:
        raise OSError("disk full")

    with patch.object(sidecar_mod, "append_receipt", side_effect=boom):
        receipt = svc.apply_plan(plan)
    assert any("sidecar_append_failed" in e for e in receipt.errors)
    assert receipt.changed_pages, "wiki really did change"


def test_apply_plan_two_distinct_commits_produce_two_sidecar_lines(
    wiki: Wiki,
) -> None:
    """Distinct commits land distinct sidecar lines; SHA-dedup does not collapse them.

    Two ``apply_plan`` calls on the same wiki with different paths get
    distinct synthetic SHAs from the autouse mock, so the sidecar
    keeps both rows. This pins the SHA-dedup invariant at zero
    subprocess cost — ``atomic_commit``, ``_snapshot_working_tree``,
    and ``_discard_snapshot`` are all stubbed.
    """
    svc = WikiMemoryService(wiki=wiki)
    svc.register_evidence({"ref-1"})
    svc.apply_plan(_make_plan())
    other_content = "---\ntitle: Other\ntype: concept\n---\n# Other\n"
    svc.apply_plan(
        MemoryPlan(
            rationale="create entity page",
            operations=[
                PageCreate(
                    path="concepts/other.md",
                    content=other_content,
                    evidence=["ref-1"],
                )
            ],
            evidence=["ref-1"],
        )
    )
    sidecar = wiki.data_root / ".lies" / "memory_plans.jsonl"
    rows = [ln for ln in sidecar.read_text().splitlines() if ln]
    assert len(rows) == 2
    parsed = [json.loads(ln) for ln in rows]
    shas = {row["commit_sha"] for row in parsed}
    assert len(shas) == 2, "distinct commits must produce distinct SHAs"


def test_apply_plan_passes_evidence_count_to_sidecar_record(wiki: Wiki) -> None:
    """Live sidecar records must carry the same evidence_count as the commit body.

    Regression: ``apply_plan`` used to compute ``evidence_count`` for the
    commit message but call ``append_receipt(..., commit_sha)`` without the
    kwarg, leaving ``evidence_count: 0`` in every JSONL row. ``lies memory
    reconcile`` reads the correct value from the commit body; live records
    and reconciled records disagreed on the same data.
    """
    svc = WikiMemoryService(wiki=wiki)
    svc.register_evidence({"ref-1", "ref-2", "ref-3"})
    plan = MemoryPlan(
        rationale="create entity page with 3 evidence refs",
        operations=[
            PageCreate(
                path="entities/postgres.md",
                content=_SAMPLE_CONTENT,
                evidence=["ref-1", "ref-2", "ref-3"],
            )
        ],
        evidence=["ref-1", "ref-2", "ref-3"],
    )
    receipt = svc.apply_plan(plan)
    assert receipt.changed_pages, "wiki really did change"
    sidecar_path = wiki.data_root / ".lies" / "memory_plans.jsonl"
    rows = [json.loads(ln) for ln in sidecar_path.read_text().splitlines() if ln]
    assert len(rows) == 1
    assert rows[0]["evidence_count"] == 3
