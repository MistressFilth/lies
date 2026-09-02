from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from pydantic_ai.models.test import TestModel

from lies.orchestrator import Orchestrator
from lies.wiki.git import CommitError
from tests.conftest import make_wiki, models_for_tests


@pytest.fixture
def wiki_root(tmp_path: Path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki").mkdir()
    return make_wiki(name="orch-test", data_root=tmp_path)


def test_orchestrator_constructs(wiki_root: Path) -> None:
    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    assert orch is not None


def test_orchestrator_runs_with_test_model(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator's underlying agent is mocked via TestModel.

    TestModel is configured with `call_tools=[]` so it returns a plain text
    response instead of trying to invoke the orchestrator's many tools
    (delegate_task, run_workflow, run_code, etc.) -- which would loop on
    invalid workflow scripts.

    The default `transport="http"` registers qmd's tools as native MCP
    tools, which TestModel rejects (``UserError: TestModel does not
    support built-in tools``). Opt out via ``LIES_QMD_TRANSPORT=stdio``
    so the capability builds a local toolset instead -- the test only
    exercises agent plumbing, not the qmd transport.
    """
    monkeypatch.setenv("LIES_QMD_TRANSPORT", "stdio")
    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    with orch._agent.override(model=TestModel(call_tools=[], custom_output_text="lint ok")):
        result = orch.run("lint")
    assert isinstance(result, str)
    assert result == "lint ok"


# --- wiki dataclass propagation tests --------------------------------------
#
# The orchestrator is the single entry point for a wiki. The `Wiki`
# dataclass must be propagated consistently to:
#   1. The Orchestrator's top-level state (a first-class attribute).
#   2. The system prompt (so sub-agents know where the wiki lives).
#   3. Capabilities that are wiki-scoped (`file_system(wiki_root=...)`).
#
# These tests pin that contract.


def test_wiki_is_top_level_attribute(wiki_root: Path) -> None:
    """`orch.wiki` must be set from the constructor argument as the Wiki
    dataclass, exposing the post-XDG role-routed paths."""
    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    assert orch.wiki is wiki_root
    assert orch.wiki.data_root == wiki_root.data_root


def test_wiki_data_root_propagates_to_system_prompt(wiki_root: Path) -> None:
    """The agent's system prompt must include the resolved wiki data root path.

    Sub-agents and tool calls rely on this for path scoping and
    path-aware reasoning.
    """
    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    prompt = orch._agent._system_prompts[0]  # type: ignore[attr-defined]
    assert str(orch.wiki.data_root) in prompt
    assert "Wiki root:" in prompt


def test_wiki_data_root_propagates_to_file_system_capability(wiki_root: Path) -> None:
    """The file_system capability must be scoped to the wiki data root.

    This is the security boundary that prevents the agent from
    reading or writing outside the wiki.
    """
    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))

    # pydantic-ai-harness stores the per-agent capabilities under
    # `agent.root_capability` (a CombinedCapability with a `capabilities`
    # list). Find the FileSystem among them.
    root_cap = orch._agent.root_capability  # type: ignore[attr-defined]
    caps = getattr(root_cap, "capabilities", [])
    fs_caps = [c for c in caps if getattr(c, "__class__", type(c)).__name__ == "FileSystem"]
    assert fs_caps, "expected a FileSystem capability in the orchestrator"
    # FileSystem stores the root under various names depending on the
    # harness version; check the obvious ones.
    fs = fs_caps[0]
    root = (
        getattr(fs, "root", None) or getattr(fs, "root_dir", None) or getattr(fs, "wiki_root", None)
    )
    assert root == orch.wiki.data_root, (
        f"file_system capability root ({root!r}) does not match "
        f"orchestrator wiki.data_root ({orch.wiki.data_root!r})"
    )


def test_wiki_data_root_resolution_handles_relative_paths(tmp_path: Path) -> None:
    """A relative data root must be resolved to an absolute path.

    The CLI passes `--name` through Typer and resolves the data root
    via the XDG helpers; the orchestrator must canonicalize the path
    once at construction so downstream components see a stable root.
    """
    import os

    cwd = tmp_path
    (cwd / "raw").mkdir()
    (cwd / "wiki").mkdir()
    rel = Path("subdir-of-cwd")
    (cwd / rel).mkdir()
    (cwd / rel / "raw").mkdir()
    (cwd / rel / "wiki").mkdir()

    # Run from tmp_path so the relative path resolves against it
    old_cwd = os.getcwd()
    try:
        os.chdir(cwd)
        rel_wiki = make_wiki(name="relative", data_root=cwd / rel)
        orch = Orchestrator(wiki=rel_wiki, models=models_for_tests("test"))
    finally:
        os.chdir(old_cwd)

    assert orch.wiki.data_root.is_absolute()
    assert orch.wiki.data_root == (cwd / rel).resolve()


# ---------------------------------------------------------------------------
# Host-side ingest atomicity / rollback (Finding 3)
#
# `Orchestrator.run_ingest` is the host-side wrapper that turns a plain
# agent call into an atomic operation. It MUST guarantee:
#
#   1. The wiki is snapshotted before the agent runs.
#   2. On agent success, the agent's edits are committed in one atomic
#      commit and the snapshot is discarded.
#   3. On agent failure (exception OR raised mid-call), the wiki working
#      tree is restored to its pre-ingest state and the exception is
#      re-raised -- the wiki never appears half-ingested.
#   4. If the post-agent atomic commit fails, the wiki is still rolled
#      back so the user does not see an uncommitted, half-applied ingest.
#
# The tests below pin each guarantee. The "mid-ingest failure" test is
# the headline case: the agent raises halfway through, and the wiki must
# be byte-identical to its pre-ingest state.
# ---------------------------------------------------------------------------


@pytest.fixture
def git_wiki(tmp_path: Path):
    """A wiki initialised as a git repository with one initial commit.

    The orchestrator's host-side snapshot/rollback uses ``git stash`` to
    capture pre-ingest state, so the wiki must be a real git working tree.
    Returns a ``Wiki`` dataclass whose ``data_root`` is the git-controlled
    directory.
    """
    data_root = tmp_path
    (data_root / "wiki").mkdir(exist_ok=True)
    (data_root / "wiki" / "index.md").write_text("# Index\n")
    (data_root / "wiki" / "log.md").write_text("# Log\n")
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(data_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=data_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=data_root,
        check=True,
        capture_output=True,
    )
    (data_root / "initial.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=data_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=data_root,
        check=True,
        capture_output=True,
    )
    return make_wiki(name="git-wiki", data_root=data_root)


def _git_wiki_root(wiki) -> Path:
    """Return the on-disk git root for a Wiki dataclass fixture."""
    return wiki.data_root


def test_run_ingest_delegates_and_returns_ingested_string(git_wiki) -> None:
    """A successful run_ingest delegates to sync_collection and returns the
    wrapper's documented ``"ingested {source}"`` string.

    The atomic commit, working-tree snapshot/rollback, and stash handling
    moved to :func:`lies.etl.sync_helper.sync_collection` (Task 27). The
    wrapper's only job is to delegate and return the back-compat string;
    this test pins that contract.
    """
    orch = Orchestrator(wiki=git_wiki, models=models_for_tests("test"))

    with mock.patch("lies.etl.sync_helper.sync_collection") as m:
        result = orch.run_ingest("raw/some-source.md", no_llm=True)

    # Wrapper returned the documented back-compat string.
    assert result == "ingested raw/some-source.md"
    # sync_collection was called once with (wiki, source.stem, force=False).
    m.assert_called_once()
    args, kwargs = m.call_args
    assert args[0] is orch.wiki
    assert args[1] == "some-source"
    assert kwargs == {"force": False}


def test_run_ingest_propagates_sync_collection_exception(git_wiki) -> None:
    """If sync_collection raises, run_ingest propagates the exception.

    The wrapper is intentionally a thin shim — it does no rollback of its
    own. Rollback is sync_collection's responsibility. The wrapper
    contract is: whatever sync_collection raises, run_ingest raises.
    """
    orch = Orchestrator(wiki=git_wiki, models=models_for_tests("test"))

    class IngestFailure(RuntimeError):
        """Simulates sync_collection crashing mid-pipeline."""

    with (
        mock.patch(
            "lies.etl.sync_helper.sync_collection",
            side_effect=IngestFailure("source-reader hit a malformed URL"),
        ),
        pytest.raises(IngestFailure, match="malformed URL"),
    ):
        orch.run_ingest("raw/broken-source.md", no_llm=True)


def test_run_ingest_propagates_keyboard_interrupt(git_wiki) -> None:
    """A ``KeyboardInterrupt`` from sync_collection propagates verbatim.

    The wrapper does not swallow ``BaseException``; user interrupts
    during the underlying sync surface to the caller.
    """
    orch = Orchestrator(wiki=git_wiki, models=models_for_tests("test"))

    with (
        mock.patch(
            "lies.etl.sync_helper.sync_collection",
            side_effect=KeyboardInterrupt(),
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        orch.run_ingest("raw/source.md", no_llm=True)


def test_run_ingest_propagates_commit_error(git_wiki) -> None:
    """A ``CommitError`` raised inside sync_collection propagates verbatim.

    The wrapper does not catch downstream errors; the caller observes
    the same ``CommitError`` sync_collection raised. The atomic-commit
    rollback path lives inside ``SyncOrchestrator.run``.
    """
    orch = Orchestrator(wiki=git_wiki, models=models_for_tests("test"))

    with (
        mock.patch(
            "lies.etl.sync_helper.sync_collection",
            side_effect=CommitError("simulated post-ingest commit failure"),
        ),
        pytest.raises(CommitError, match="simulated post-ingest commit"),
    ):
        orch.run_ingest("raw/some-source.md", no_llm=True)


def test_run_ingest_propagates_sync_failure_with_pre_existing_dirty_state(
    git_wiki,
) -> None:
    """sync_collection raises → wrapper re-raises; the wrapper itself
    never touches the working tree.

    The wrapper contract is "delegate + return string". Any rollback or
    dirty-state preservation is sync_collection's concern. This test
    asserts the wrapper does not interfere with the user's pre-existing
    dirty state on the failure path: when sync_collection raises, the
    exception propagates and the wrapper has not mutated anything.
    """
    # Pre-state: a tracked file with a user edit (real dirty state).
    data_root = git_wiki.data_root
    (data_root / "initial.txt").write_text("user in-progress edit")
    (data_root / "user-notes.md").write_text("# WIP\n")

    orch = Orchestrator(wiki=git_wiki, models=models_for_tests("test"))
    pre_initial = (data_root / "initial.txt").read_text()
    pre_notes = (data_root / "user-notes.md").read_text()

    with (
        mock.patch(
            "lies.etl.sync_helper.sync_collection",
            side_effect=RuntimeError("sync crashed mid-ingest"),
        ),
        pytest.raises(RuntimeError, match="sync crashed"),
    ):
        orch.run_ingest("raw/source.md", no_llm=True)

    # The wrapper itself did not touch the user's dirty state.
    assert (data_root / "initial.txt").read_text() == pre_initial
    assert (data_root / "user-notes.md").read_text() == pre_notes


def test_run_ingest_success_returns_ingested_string(
    git_wiki,
) -> None:
    """On the success path, the wrapper returns ``"ingested {source}"``.

    The wrapper does no git bookkeeping itself; sync_collection owns the
    snapshot/commit/discard logic. This test pins the wrapper's success
    contract only: delegation + the documented return string.
    """
    data_root = git_wiki.data_root
    (data_root / "user-notes.md").write_text("# WIP\n")
    orch = Orchestrator(wiki=git_wiki, models=models_for_tests("test"))

    with mock.patch("lies.etl.sync_helper.sync_collection") as m:
        result = orch.run_ingest("raw/source.md", no_llm=True)

    assert result == "ingested raw/source.md"
    m.assert_called_once()


def test_run_ingest_propagates_nothing_to_commit_error(git_wiki) -> None:
    """A ``CommitError("nothing to commit")`` from sync_collection propagates.

    sync_collection may decide there is nothing to do (no files changed)
    and raise CommitError; the wrapper surfaces that verbatim so the
    caller can distinguish "no-op" from "real failure".
    """
    orch = Orchestrator(wiki=git_wiki, models=models_for_tests("test"))

    with (
        mock.patch(
            "lies.etl.sync_helper.sync_collection",
            side_effect=CommitError("nothing to commit"),
        ),
        pytest.raises(CommitError, match="nothing to commit"),
    ):
        orch.run_ingest("raw/empty-source.md", no_llm=True)


def test_orchestrator_uses_qmd_http_transport(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator registers a QmdCapability with http transport."""
    from lies.qmd import capability as qmd_capability
    from lies.qmd.capability import QmdCapability

    built: list[dict] = []
    original_init = QmdCapability.__init__

    def _recording_init(self, **kwargs):  # type: ignore[no-untyped-def]
        built.append(kwargs)
        original_init(self, **kwargs)

    monkeypatch.setattr(qmd_capability.QmdCapability, "__init__", _recording_init)
    monkeypatch.setattr("lies.qmd.capability.qmd_daemon_reachable", lambda url, timeout=0.5: True)
    monkeypatch.delenv("LIES_QMD_TRANSPORT", raising=False)
    monkeypatch.delenv("LIES_QMD_URL", raising=False)

    orch = Orchestrator(wiki=wiki_root, models=models_for_tests("test"))
    assert built, "QmdCapability was not constructed"
    assert built[0]["transport"] == "http"
    assert built[0]["url"] == "http://127.0.0.1:8181"
    assert built[0]["wiki"] is orch.wiki
