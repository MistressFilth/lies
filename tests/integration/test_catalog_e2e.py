"""End-to-end integration tests for the catalog port. Gated on INTEGRATION=1.

Exercises the catalog surface end-to-end against a real
``WikiMemoryService`` + real cross-process flock + mocked qmd:

1. ``apply_plan(PageCreate)`` writes a catalog row alongside the page
   file and the git commit lands with the new path.
2. ``reconcile(wiki)`` removes a catalog row whose on-disk file is
   absent (orphan), exercising the dangling-row cleanup path.
3. ``open_catalog`` on a wiki whose ``catalog.db`` does not yet exist
   backfills the catalog from the existing on-disk markdown files
   (first-open migration path).

qmd is mocked because the integration gating exists to keep the unit
suite independent of a running qmd daemon; the catalog port itself
does not depend on qmd for these flows. ``WikiMemoryService`` is
real (real flock, real ``atomic_commit``, real per-op catalog
upsert inside the existing snapshot+restore envelope).

Run with:

    INTEGRATION=1 pytest tests/integration/test_catalog_e2e.py -v

Without ``INTEGRATION=1``, the module skips cleanly so the unit suite
is not coupled to the cross-process flock or to a live git working
tree under ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from lies.memory.catalog import list_pages, open_catalog, reconcile
from lies.memory.catalog_models import PageSection
from lies.memory.models import MemoryPlan, PageCreate, WikiCommitFailed
from lies.memory.service import WikiMemoryService
from tests.conftest import make_wiki

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION") != "1",
    reason="gated on INTEGRATION=1 (real flock + real wiki memory service)",
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def _skip_qmd_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the post-commit qmd refresh with a no-op.

    The catalog port does not depend on qmd, but
    ``WikiMemoryService.apply_plan`` calls ``qmd_update`` after every
    commit so the derived index stays fresh. Mocking it out keeps the
    test independent of a live qmd daemon while preserving the
    ``WikiMemoryService`` envelope (flock, snapshot, atomic commit,
    per-op catalog upsert) end-to-end.
    """
    monkeypatch.setattr(WikiMemoryService, "_refresh_qmd", lambda self: (True, ""))


@pytest.fixture
def wiki_dir(tmp_path: Path, _skip_qmd_refresh: None) -> Path:
    """A real git wiki rooted in ``tmp_path`` with all five role roots set.

    Mirrors the bootstrap shape used by ``test_synthesis_file_back``:
    create the directory layout, ``git init``, configure a committer,
    then wire the Wiki through ``make_wiki`` so the XDG-redirected
    config / cache / state / runtime roots are populated. The catalog
    ``.lies/`` directory under ``wiki.wiki_dir`` is created lazily by
    :func:`open_catalog` on first open; we do not seed it here so the
    backfill test exercises the "fresh wiki" path.
    """
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return root


def _wiki(data_root: Path):
    """Build a :class:`Wiki` rooted at ``data_root`` (tmp_path flavor)."""
    return make_wiki(name="catalog-e2e", data_root=data_root)


def _create_page_plan(path: str, *, title: str, body: str) -> MemoryPlan:
    """Build a single-op ``MemoryPlan`` with one ``PageCreate``.

    ``MemoryPlan.evidence`` is required (min_length=1) and so is
    each op's ``evidence`` list; both are seeded with a synthetic
    reference ``f"seed:{path}"`` unique per page so cross-page tests
    stay isolated. Callers must ``service.register_evidence({ref})``
    before invoking ``service.apply_plan`` so
    :func:`validate_operation_evidence` accepts the reference.
    """
    return MemoryPlan(
        operations=[
            PageCreate(
                path=path,
                content=f"---\ntitle: {title}\ntype: concept\n---\n\n{body}\n",
                evidence=[f"seed:{path}"],
                tag="memory",
            ),
        ],
        rationale=f"create {path}",
        evidence=[f"seed:{path}"],
    )


# ---------------------------------------------------------------------------
# 1. apply_plan writes the catalog row + commits
# ---------------------------------------------------------------------------


def test_apply_plan_writes_catalog_row(wiki_dir: Path) -> None:
    """``apply_plan(PageCreate)`` writes the file, commits, and upserts a catalog row.

    Exercises the per-op catalog upsert path inside the existing
    snapshot+atomic-commit envelope: the page file lands on disk, a
    git commit records it, and ``catalog.db`` holds a row with the
    expected slug, title, type, and source_pkg.
    """
    wiki = _wiki(wiki_dir)
    service = WikiMemoryService(wiki)
    service.register_evidence({"seed:claude-code/concepts/hooks.md"})

    plan = _create_page_plan(
        path="claude-code/concepts/hooks.md",
        title="Hooks",
        body="Hooks bind lifecycle events.",
    )
    receipt = service.apply_plan(plan)

    assert receipt.errors == [], f"unexpected errors: {receipt.errors!r}"
    assert any(ref.path == "claude-code/concepts/hooks.md" for ref in receipt.changed_pages)

    # The page file landed on disk and was committed.
    page_file = wiki.wiki_dir / "claude-code" / "concepts" / "hooks.md"
    assert page_file.exists(), (
        f"expected {page_file} to exist; wiki_dir={wiki.wiki_dir!r}, "
        f"changed_pages={[r.path for r in receipt.changed_pages]!r}"
    )
    git_log = subprocess.run(
        ["git", "log", "--pretty=format:%s", "-1"],
        cwd=wiki_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "create claude-code/concepts/hooks" in git_log.stdout

    # The catalog holds exactly the expected row.
    conn = open_catalog(wiki)
    try:
        pages = list_pages(conn)
    finally:
        conn.close()
    slugs = {p.slug for p in pages}
    assert "claude-code/concepts/hooks" in slugs
    page = next(p for p in pages if p.slug == "claude-code/concepts/hooks")
    assert page.title == "Hooks"
    assert page.type == "concept"
    assert page.source_pkg == "claude-code"
    assert page.section == PageSection.wiki


# ---------------------------------------------------------------------------
# 2. snapshot/restore leaves an orphan; reconcile cleans it
# ---------------------------------------------------------------------------


def test_snapshot_restore_leaves_orphan_reconcile_cleans(
    wiki_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``atomic_commit`` leaves a dangling catalog row that ``reconcile`` cleans.

    Drives the snapshot/restore envelope of
    ``WikiMemoryService.apply_plan`` end-to-end. The per-op catalog
    upsert (``service.py`` line ~828) commits BEFORE
    ``atomic_commit`` (line ~667); when the commit raises
    :class:`WikiCommitFailed` the on-disk page file is rolled back to
    its pre-apply state via ``_restore_working_tree``, but the catalog
    row the per-op upsert already wrote remains — orphaned against the
    now-empty file. ``reconcile(wiki, dry_run=False)`` then cleans the
    dangling row in a follow-up call.

    The failure is forced by monkey-patching ``atomic_commit`` (the
    post-file-write commit step) to raise ``WikiCommitFailed``. A
    ``WikiWriteConflict`` (hash mismatch on ``PageUpdate``) would be
    rejected earlier at ``validate_plan`` and the catalog row would
    never be written — that path can't exercise the snapshot/restore
    envelope, so it is not the failure we want here.

    The wiki's ``.gitignore`` must list ``.lies/`` (and by extension
    ``catalog.db``) so ``git stash push --include-untracked``
    (snapshot) and ``git clean -fd`` (restore) leave the catalog file
    alone. Without that ignore, the snapshot+restore envelope would
    hide the orphan by removing the new ``catalog.db`` alongside the
    rolled-back page file, defeating the test.
    """
    # Seed a .gitignore so catalog.db is excluded from snapshot+restore.
    # The bootstrap (``lies init``) writes the same line; replicating it
    # here keeps the test self-contained and explicit about why the
    # catalog file survives the restore.
    (wiki_dir / ".gitignore").write_text(".lies/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=wiki_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "gitignore"],
        cwd=wiki_dir,
        check=True,
        capture_output=True,
    )

    wiki = _wiki(wiki_dir)
    service = WikiMemoryService(wiki)
    service.register_evidence({"seed:claude-code/concepts/hooks.md"})

    plan = _create_page_plan(
        path="claude-code/concepts/hooks.md",
        title="Hooks",
        body="Hooks bind lifecycle events.",
    )

    # Patch the post-write commit step to fail. The file write and the
    # per-op catalog upsert already landed inside ``_apply_operations``;
    # only the git commit fails. The snapshot/restore envelope rolls
    # the file back, but the catalog row the upsert wrote stays.
    def _failing_commit(*args: object, **kwargs: object) -> None:
        raise WikiCommitFailed("simulated commit failure")

    monkeypatch.setattr("lies.memory.service.atomic_commit", _failing_commit)

    with pytest.raises(WikiCommitFailed):
        service.apply_plan(plan)

    # The page file was rolled back to its pre-apply state (the wiki
    # only had ``wiki/index.md`` + ``.gitignore`` from the fixture).
    page_file = wiki.wiki_dir / "claude-code" / "concepts" / "hooks.md"
    assert not page_file.exists(), f"snapshot/restore should have rolled back {page_file}"

    # But the catalog row remains — orphaned by the partial commit.
    conn = open_catalog(wiki)
    try:
        slugs_after_fail = {p.slug for p in list_pages(conn)}
    finally:
        conn.close()
    assert "claude-code/concepts/hooks" in slugs_after_fail, (
        f"expected orphan row 'claude-code/concepts/hooks' to remain "
        f"after failed commit; got slugs={sorted(slugs_after_fail)!r}"
    )

    # Reconcile cleans the dangling row.
    result = reconcile(wiki, dry_run=False)
    assert result.removed >= 1

    conn = open_catalog(wiki)
    try:
        slugs_after_reconcile = {p.slug for p in list_pages(conn)}
    finally:
        conn.close()
    assert "claude-code/concepts/hooks" not in slugs_after_reconcile


# ---------------------------------------------------------------------------
# 3. first open_catalog on a wiki with pre-existing files backfills
# ---------------------------------------------------------------------------


def test_first_open_backfills_existing_files(wiki_dir: Path) -> None:
    """``open_catalog`` on a wiki with no ``catalog.db`` backfills from disk.

    The backfill walks ``wiki.wiki_dir``, computes a slug per
    ``.md`` file (excluding system files), and seeds the ``pages``
    table. This is the migration path for a wiki that had pages
    written before the catalog existed; without it the first
    ``list_pages`` call after the port would return an empty result
    set even though ``wiki.wiki_dir`` already had content.
    """
    # Pre-write a page BEFORE opening the catalog. Mimics a wiki
    # whose markdown pre-dates the catalog port.
    page_dir = wiki_dir / "wiki" / "claude-code"
    page_dir.mkdir(parents=True, exist_ok=True)
    page_file = page_dir / "pre-existing.md"
    page_file.write_text(
        "---\ntitle: Pre-existing\ntype: concept\n---\n\n# Pre\n",
        encoding="utf-8",
    )

    # Confirm there is no catalog file yet — the backfill must run
    # against a "fresh" wiki state.
    catalog_path = wiki_dir / "wiki" / ".lies" / "catalog.db"
    assert not catalog_path.exists()

    wiki = _wiki(wiki_dir)
    conn = open_catalog(wiki)  # first-open: triggers backfill
    try:
        pages = list_pages(conn)
    finally:
        conn.close()

    slugs = {p.slug for p in pages}
    assert "claude-code/pre-existing" in slugs
    page = next(p for p in pages if p.slug == "claude-code/pre-existing")
    assert page.title == "Pre-existing"
    assert page.type == "concept"
    assert page.source_pkg == "claude-code"
    # Catalog file now exists; the WAL siblings live alongside it.
    assert catalog_path.exists()
