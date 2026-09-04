"""End-to-end integration tests for ``Orchestrator.run_ingest`` (F2 default route).

Task 10 rewires ``run_ingest`` to use the LLM round-trip end-to-end.
The default (``no_llm=False``) flow:

    1. materialize ``source`` to ``raw/<collection>/<basename>``
    2. ``source_reader_agent`` -> ``SourceExtraction``
    3. ``page_writer_agent`` -> ``list[PageDiff]``
    4. translate -> ``MemoryPlan(tag="ingest")``
    5. ``WikiMemoryService.apply_plan`` (atomic commit, sidecar, log,
       qmd refresh)

On agent failure, the wrapper quarantines the source and raises
:class:`IngestQuarantined`. The agent factories are monkeypatched so no
real LLM call ever fires; the wrapping ``Orchestrator`` /
``WikiMemoryService`` machinery runs for real so the test catches
integration-level regressions (snapshot lifecycle, evidence
registration, atomic commit shape, sidecar + log appends).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lies.agents.page_writer import PageDiff, PageOperation
from lies.agents.source_reader import SourceExtraction
from lies.memory.models import (
    IngestQuarantined,
    IngestSourceUnreachable,
    WikiPlanInvalid,
)
from lies.orchestrator import Orchestrator
from tests.conftest import make_wiki, models_for_tests

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample-wiki"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def wiki_copy(tmp_path: Path) -> Path:
    """Copy the sample fixture wiki to ``tmp_path`` and init git there.

    Mirrors :func:`tests.integration.test_end_to_end.wiki_copy`; we
    cannot import that fixture directly because pytest fixture lookup
    is per-file.
    """
    target = tmp_path / "wiki"
    shutil.copytree(FIXTURE, target)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return target


def _make_wiki(data_root: Path):
    """Build a :class:`Wiki` rooted at ``data_root``.

    Same shape as :func:`tests.unit.test_call_page_writer._make_wiki`
    but parameterized on a caller-supplied path so the fixture and
    ``tmp_path`` flavors share one helper.
    """
    (data_root / "wiki").mkdir(parents=True, exist_ok=True)
    (data_root / "raw").mkdir(parents=True, exist_ok=True)
    return make_wiki(name="ingest-end-to-end", data_root=data_root)


class _FakeResult:
    """Mimics pydantic-ai's ``AgentRunResult`` enough for ``.output``."""

    def __init__(self, output: object) -> None:
        self.output = output


class _FakeAgent:
    """Drop-in agent that returns ``output`` or raises ``raise_on_run``."""

    def __init__(
        self,
        output: object | None = None,
        raise_on_run: BaseException | None = None,
    ) -> None:
        self.output = output
        self.raise_on_run = raise_on_run

    def run_sync(self, *args: object, **kwargs: object) -> _FakeResult:
        if self.raise_on_run is not None:
            raise self.raise_on_run
        return _FakeResult(self.output)


def _fake_extraction() -> SourceExtraction:
    """A deterministic :class:`SourceExtraction` for the success-path tests."""
    return SourceExtraction(
        claims=["Hooks bind lifecycle events"],
        entities=["Hooks"],
        concepts=["Hooks"],
        comparisons=[],
        summary="Hooks bind Claude Code lifecycle events.",
    )


def _make_source(tmp_path: Path, wiki_data_root: Path) -> Path:
    """Pre-stage ``article.md`` outside the wiki so it survives the snapshot.

    ``_materialize_source`` then copies it into ``raw/<collection>/``
    where the agent pipeline expects to find it. Pre-staging inside the
    wiki would put the file inside the git working tree, where the
    orchestrator-level snapshot would stash it before the agent reads
    it; staging outside sidesteps that conflict while still exercising
    the materialize-then-quarantine path.
    """
    src = tmp_path / "article.md"
    src.write_text("# Article body\n", encoding="utf-8")
    return src


# ---------------------------------------------------------------------------
# Step 10.1 — integration tests
# ---------------------------------------------------------------------------


def test_run_ingest_happy_path_writes_pages_and_commits(
    monkeypatch: pytest.MonkeyPatch,
    wiki_copy: Path,
) -> None:
    """End-to-end success: agent -> PageDiff -> plan -> apply -> commit.

    The collection is derived from ``Path(source).stem`` (so it picks
    up the source file's basename, not its parent directory). The
    page-writer can still target any ``wiki/<...>/<...>.md`` path.
    """
    wiki = _make_wiki(wiki_copy)
    src = _make_source(wiki_copy.parent, wiki.data_root)
    monkeypatch.setattr(
        "lies.orchestrator.source_reader_agent",
        lambda model: _FakeAgent(output=_fake_extraction()),
    )
    monkeypatch.setattr(
        "lies.orchestrator.page_writer_agent",
        lambda model: _FakeAgent(
            output=[
                PageDiff(
                    operation=PageOperation.CREATE,
                    path=Path("article/concepts/hooks.md"),
                    new_content=("---\ntitle: Hooks\ntype: concept\n---\n# Hooks\n\nbody\n"),
                )
            ]
        ),
    )
    o = Orchestrator(wiki, models=models_for_tests("test"))
    result = o.run_ingest(str(src))
    # Back-compat-ish return string: "ingested {source} into {collection}".
    assert "article.md" in result
    assert "article" in result

    target = wiki.wiki_dir / "article" / "concepts" / "hooks.md"
    assert target.exists()

    # Sidecar appended one row referencing the new page.
    sidecar = wiki.data_root / ".lies" / "memory_plans.jsonl"
    assert sidecar.exists()
    rows = [
        json.loads(line)
        for line in sidecar.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any("hooks.md" in row.get("pages", [""])[0] for row in rows)

    # Log entry written with tag=ingest.
    log = (wiki.wiki_dir / "log.md").read_text(encoding="utf-8")
    assert "ingest |" in log


def test_run_ingest_happy_path_with_wiki_prefix(
    monkeypatch: pytest.MonkeyPatch,
    wiki_copy: Path,
) -> None:
    """End-to-end success when the agent emits paths WITH the ``wiki/`` prefix.

    The page-writer prompt tells the LLM to emit paths in
    ``wiki/<collection>/<type>/<name>.md`` form per the schema
    convention. ``WikiMemoryService._apply_operations`` strips the
    leading ``wiki/`` before resolving so the file lands at
    ``<wiki.wiki_dir>/<collection>/<type>/<name>.md`` rather than the
    doubled-prefix path
    ``<wiki.wiki_dir>/wiki/<collection>/<type>/<name>.md``. This test
    covers the realistic LLM shape that ``test_run_ingest_happy_path_writes_pages_and_commits``
    sidesteps by emitting an unprefixed path.
    """
    wiki = _make_wiki(wiki_copy)
    src = _make_source(wiki_copy.parent, wiki.data_root)
    monkeypatch.setattr(
        "lies.orchestrator.source_reader_agent",
        lambda model: _FakeAgent(output=_fake_extraction()),
    )
    monkeypatch.setattr(
        "lies.orchestrator.page_writer_agent",
        lambda model: _FakeAgent(
            output=[
                PageDiff(
                    operation=PageOperation.CREATE,
                    path=Path("wiki/article/concepts/alpha.md"),
                    new_content=("---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\nbody\n"),
                )
            ]
        ),
    )
    o = Orchestrator(wiki, models=models_for_tests("test"))
    result = o.run_ingest(str(src))
    assert "article.md" in result

    # The file lands at the per-collection subdir, not the doubled-prefix
    # ``<wiki.wiki_dir>/wiki/<...>`` location.
    target = wiki.wiki_dir / "article" / "concepts" / "alpha.md"
    assert target.exists()
    # Belt-and-suspenders: the doubled-prefix path must NOT exist.
    doubled = wiki.wiki_dir / "wiki" / "article" / "concepts" / "alpha.md"
    assert not doubled.exists()


def test_run_ingest_with_wiki_prefix_full_envelope(
    monkeypatch: pytest.MonkeyPatch,
    wiki_copy: Path,
) -> None:
    """Prefixed-path E2E: sidecar + log entry + index all reflect the write.

    Extends ``test_run_ingest_happy_path_with_wiki_prefix`` to assert
    the surrounding machinery (sidecar row, ``wiki/log.md`` entry,
    rebuilt ``wiki/index.md`` listing the new page) all reach disk for
    the prefixed-path case. Without the strip-prefix fix in
    ``_apply_operations`` / ``_collect_commit_files``, ``git add``
    fails to find the file at the doubled-prefix path and the commit
    raises ``CommitError`` — none of these artifacts would land.
    """
    wiki = _make_wiki(wiki_copy)
    src = _make_source(wiki_copy.parent, wiki.data_root)
    monkeypatch.setattr(
        "lies.orchestrator.source_reader_agent",
        lambda model: _FakeAgent(output=_fake_extraction()),
    )
    monkeypatch.setattr(
        "lies.orchestrator.page_writer_agent",
        lambda model: _FakeAgent(
            output=[
                PageDiff(
                    operation=PageOperation.CREATE,
                    path=Path("wiki/article/concepts/alpha.md"),
                    new_content=("---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\nbody\n"),
                )
            ]
        ),
    )
    o = Orchestrator(wiki, models=models_for_tests("test"))
    o.run_ingest(str(src))

    # Sidecar row references the page.
    sidecar = wiki.data_root / ".lies" / "memory_plans.jsonl"
    assert sidecar.exists()
    rows = [
        json.loads(line)
        for line in sidecar.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any("alpha.md" in row.get("pages", [""])[0] for row in rows)

    # Log entry with tag=ingest.
    log = (wiki.wiki_dir / "log.md").read_text(encoding="utf-8")
    assert "ingest |" in log
    assert "alpha.md" in log

    # As of the F4b+F16 catalog port, the service no longer calls
    # ``rebuild_index`` after the commit. Per-op catalog upserts keep
    # ``catalog.db`` in lockstep with the wiki instead, so a freshly
    # ingested concept is visible to the catalog immediately even
    # though ``wiki/index.md`` is not regenerated by the apply envelope.
    from lies.memory.catalog import list_pages, open_catalog

    conn = open_catalog(wiki)
    try:
        slugs = {p.slug for p in list_pages(conn)}
    finally:
        conn.close()
    assert any(slug.endswith("alpha") for slug in slugs)


def test_run_ingest_quarantines_on_page_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
    wiki_copy: Path,
) -> None:
    """Page-writer raises -> quarantine + raise ``IngestQuarantined``.

    Collection name derives from ``Path(source).stem`` so the poison
    sidecar lives under ``<collection>/<basename>``.
    """
    wiki = _make_wiki(wiki_copy)
    src = _make_source(wiki_copy.parent, wiki.data_root)
    monkeypatch.setattr(
        "lies.orchestrator.source_reader_agent",
        lambda model: _FakeAgent(output=_fake_extraction()),
    )
    monkeypatch.setattr(
        "lies.orchestrator.page_writer_agent",
        lambda model: _FakeAgent(raise_on_run=ValueError("rate limit")),
    )
    o = Orchestrator(wiki, models=models_for_tests("test"))
    with pytest.raises(IngestQuarantined):
        o.run_ingest(str(src))
    poison = wiki.poison_root / "article" / "article.md"
    assert poison.exists()


def test_run_ingest_no_llm_falls_back_to_sync_collection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``no_llm=True`` preserves the legacy shim; the LLM path is not taken."""
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "README").write_text("seed", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    wiki = _make_wiki(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "lies.etl.sync_helper.sync_collection",
        lambda w, name, *, force=False: captured.setdefault("called", (w, name, force)),
    )
    o = Orchestrator(wiki, models=models_for_tests("test"))
    out = o.run_ingest("raw/claude-code/article.md", no_llm=True)
    assert captured["called"][1] == "article"  # derived from Path(source).stem
    assert "article.md" in out


def test_run_ingest_unreachable_source_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing local source raises ``IngestSourceUnreachable`` before
    any snapshot is taken or any agent is called.

    The wiki is initialized as a git repo so the orchestrator's
    snapshot (taken before materialize in the production code path)
    can run; the unreachable source still raises during materialize
    and we only care that the typed error propagates.
    """
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    # git stash refuses to run without an initial commit, so seed one.
    (tmp_path / "README").write_text("seed", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    wiki = _make_wiki(tmp_path)
    o = Orchestrator(wiki, models=models_for_tests("test"))
    # Ensure neither agent is invoked: if it were, _FakeAgent would
    # fail because it was never patched in.
    monkeypatch.setattr(
        "lies.orchestrator.source_reader_agent",
        lambda model: (_ for _ in ()).throw(AssertionError("source reader must not be called")),
    )
    monkeypatch.setattr(
        "lies.orchestrator.page_writer_agent",
        lambda model: (_ for _ in ()).throw(AssertionError("page writer must not be called")),
    )
    with pytest.raises(IngestSourceUnreachable):
        o.run_ingest("/definitely/does/not/exist.md")


def test_run_ingest_unreachable_source_discards_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unreachable source must drop the snapshot, not leak it.

    Regression for the whole-branch review finding: ``_materialize_source``
    raises ``IngestSourceUnreachable`` AFTER the orchestrator took a
    stash snapshot of pre-existing dirty state. Before the fix the
    exception bypassed both ``except IngestQuarantined`` (discard) and
    ``except BaseException`` (restore) arms and propagated raw,
    leaving the stash entry in ``git stash list`` until a future
    ``git stash clear`` or another ``run_ingest`` overwrote it.

    The test pre-stages a tracked file with an uncommitted edit so
    ``git stash push`` records a real entry. After the unreachable
    raise, ``git stash list`` must be empty — the snapshot was
    discarded on the unreachable path.
    """
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    # Seed an initial commit so ``git stash push`` is willing to run.
    (tmp_path / "README").write_text("seed", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    # Pre-existing dirty edit so the snapshot records a real entry.
    (tmp_path / "README").write_text("seed with edit", encoding="utf-8")
    wiki = _make_wiki(tmp_path)
    o = Orchestrator(wiki, models=models_for_tests("test"))
    monkeypatch.setattr(
        "lies.orchestrator.source_reader_agent",
        lambda model: (_ for _ in ()).throw(AssertionError("source reader must not be called")),
    )
    monkeypatch.setattr(
        "lies.orchestrator.page_writer_agent",
        lambda model: (_ for _ in ()).throw(AssertionError("page writer must not be called")),
    )
    with pytest.raises(IngestSourceUnreachable):
        o.run_ingest("/definitely/does/not/exist.md")
    stash_list = subprocess.run(
        ["git", "stash", "list"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert stash_list.stdout.strip() == "", (
        f"stash entry leaked after unreachable source: {stash_list.stdout!r}"
    )


def test_run_ingest_update_with_wiki_prefix_sha_lookup_matches(
    monkeypatch: pytest.MonkeyPatch,
    wiki_copy: Path,
) -> None:
    """Agent UPDATE on a prefixed path: ``_sha_lookup`` must compute the
    real on-disk hash, not ``""``.

    Regression for the whole-branch review finding: ``_sha_lookup``
    passed the prefixed path straight to ``_read_page``, which joins
    onto ``wiki.wiki_dir``. With a ``"wiki/<col>/<file>.md"`` input
    the lookup read ``<data_root>/wiki/wiki/<col>/<file>.md`` (which
    does not exist), returned ``""``, and the orchestrator stamped
    ``expected_sha256=""`` onto the ``PageUpdate``. The apply path
    stripped the ``wiki/`` prefix and computed the real on-disk hash,
    so ``validate_plan`` raised :class:`WikiWriteConflict` on a
    well-formed update.

    The CREATE-with-prefix sibling tests cover the on-disk write
    landing at the correct location; this test specifically exercises
    the UPDATE-with-prefix path that was silently broken.
    """
    import hashlib

    wiki = _make_wiki(wiki_copy)
    src = _make_source(wiki_copy.parent, wiki.data_root)

    # Pre-stage a page at the correct on-disk location so the apply
    # path computes a real hash and the sha lookup needs to agree.
    # Commit the pre-staged file so the orchestrator's pre-ingest
    # ``git stash push --include-untracked`` does not move it off
    # disk into the stash (the sha lookup would then see ``None``
    # and the test would fail for a non-fix reason).
    body = "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\nold body\n"
    (wiki.wiki_dir / "article" / "concepts").mkdir(parents=True, exist_ok=True)
    (wiki.wiki_dir / "article" / "concepts" / "alpha.md").write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wiki_copy, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed alpha"], cwd=wiki_copy, check=True, capture_output=True
    )
    real_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    monkeypatch.setattr(
        "lies.orchestrator.source_reader_agent",
        lambda model: _FakeAgent(output=_fake_extraction()),
    )
    monkeypatch.setattr(
        "lies.orchestrator.page_writer_agent",
        lambda model: _FakeAgent(
            output=[
                PageDiff(
                    operation=PageOperation.UPDATE,
                    path=Path("wiki/article/concepts/alpha.md"),
                    old_content=body,
                    new_content=body + "\n## New section\n",
                )
            ]
        ),
    )
    o = Orchestrator(wiki, models=models_for_tests("test"))
    # Before the fix this raised ``WikiWriteConflict`` (``expected
    # `` got ``<real-hash-prefix>``). After the fix the lookup matches
    # the apply path's hash and the run completes.
    o.run_ingest(str(src))

    updated = (wiki.wiki_dir / "article" / "concepts" / "alpha.md").read_text(encoding="utf-8")
    assert "## New section" in updated
    # Belt-and-suspenders: the file at the doubled-prefix path still
    # does not exist (the UPDATE rewrote the real one, not a shadow).
    doubled = wiki.wiki_dir / "wiki" / "article" / "concepts" / "alpha.md"
    assert not doubled.exists()
    # Reference ``real_hash`` so the computed value is observable in
    # the test surface; the assertion above already proves the
    # update landed.
    assert real_hash


def test_run_ingest_validation_failure_refuses_index_writes(
    monkeypatch: pytest.MonkeyPatch,
    wiki_copy: Path,
) -> None:
    """Agent emits ``CREATE wiki/index.md`` -> the service-layer guard
    raises :class:`WikiPlanInvalid` and ``run_ingest`` propagates it."""
    wiki = _make_wiki(wiki_copy)
    src = _make_source(wiki_copy.parent, wiki.data_root)
    monkeypatch.setattr(
        "lies.orchestrator.source_reader_agent",
        lambda model: _FakeAgent(output=_fake_extraction()),
    )
    monkeypatch.setattr(
        "lies.orchestrator.page_writer_agent",
        lambda model: _FakeAgent(
            output=[
                PageDiff(
                    operation=PageOperation.CREATE,
                    path=Path("wiki/index.md"),  # forbidden by service guard
                    new_content="bad",
                )
            ]
        ),
    )
    o = Orchestrator(wiki, models=models_for_tests("test"))
    with pytest.raises(WikiPlanInvalid):
        o.run_ingest(str(src))


def test_run_ingest_materialize_oserror_discards_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A raw ``OSError`` from ``_materialize_source`` must drop the snapshot,
    not leak it.

    Regression for the final-review Minor finding: ``_materialize_source``
    wraps URL / stdin / missing-local-path failures as
    :class:`IngestSourceUnreachable`, but its disk I/O calls
    (``mkdir(parents=True, exist_ok=True)``, ``target.write_text``,
    ``target.write_bytes``) can raise a raw ``PermissionError`` /
    :class:`OSError` that bypasses the wrapper. The previous
    ``except IngestSourceUnreachable`` arm did not catch that case, so
    the snapshot taken before materialize survived the raise and
    leaked into ``git stash list``.

    The test pre-stages a tracked file with an uncommitted edit so
    ``git stash push`` records a real entry, monkeypatches
    ``_materialize_source`` to raise ``PermissionError("read-only
    filesystem")``, then asserts the raw ``OSError`` propagates AND
    ``git stash list`` is empty after the call.
    """
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    # Seed an initial commit so ``git stash push`` is willing to run.
    (tmp_path / "README").write_text("seed", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    # Pre-existing dirty edit so the snapshot records a real entry.
    (tmp_path / "README").write_text("seed with edit", encoding="utf-8")
    wiki = _make_wiki(tmp_path)
    o = Orchestrator(wiki, models=models_for_tests("test"))

    # Force ``_materialize_source`` to raise a raw ``OSError`` (not the
    # typed ``IngestSourceUnreachable``) so the widened except arm in
    # ``run_ingest`` is the one that fires.
    def _boom(source: str, collection: str) -> Path:
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(o, "_materialize_source", _boom)
    monkeypatch.setattr(
        "lies.orchestrator.source_reader_agent",
        lambda model: (_ for _ in ()).throw(AssertionError("source reader must not be called")),
    )
    monkeypatch.setattr(
        "lies.orchestrator.page_writer_agent",
        lambda model: (_ for _ in ()).throw(AssertionError("page writer must not be called")),
    )
    src = _make_source(tmp_path.parent, wiki.data_root)
    with pytest.raises(PermissionError):
        o.run_ingest(str(src))
    stash_list = subprocess.run(
        ["git", "stash", "list"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert stash_list.stdout.strip() == "", (
        f"stash entry leaked after raw OSError from materialize: {stash_list.stdout!r}"
    )
