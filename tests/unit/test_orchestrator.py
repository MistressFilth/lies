from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest
from pydantic_ai.models.test import TestModel

from lies.orchestrator import Orchestrator
from lies.wiki.git import CommitError


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".lies").mkdir()
    return tmp_path


def test_orchestrator_constructs(wiki_root: Path) -> None:
    orch = Orchestrator(wiki_root=wiki_root, model="test")
    assert orch is not None


def test_orchestrator_runs_with_test_model(wiki_root: Path) -> None:
    """The orchestrator's underlying agent is mocked via TestModel.

    TestModel is configured with `call_tools=[]` so it returns a plain text
    response instead of trying to invoke the orchestrator's many tools
    (delegate_task, run_workflow, run_code, etc.) -- which would loop on
    invalid workflow scripts.
    """
    orch = Orchestrator(wiki_root=wiki_root, model="test")
    with orch._agent.override(model=TestModel(call_tools=[], custom_output_text="lint ok")):
        result = orch.run("lint")
    assert isinstance(result, str)
    assert result == "lint ok"


# --- wiki_root propagation tests --------------------------------------------
#
# The orchestrator is the single entry point for a wiki. The wiki_root
# argument must be propagated consistently to:
#   1. The Orchestrator's top-level state (a first-class attribute, not
#      nested inside `.layout`)
#   2. The on-disk layout (`WikiLayout.root` must equal `orch.wiki_root`)
#   3. The system prompt (so sub-agents know where the wiki lives)
#   4. Capabilities that are wiki-scoped (`file_system(wiki_root=...)`)
#
# These tests pin that contract.


def test_wiki_root_is_top_level_attribute(wiki_root: Path) -> None:
    """`orch.wiki_root` must be set from the constructor argument.

    Callers and tests inspect the wiki root without going through
    `orch.layout.root`. A separate `wiki_root` attribute makes that
    discoverable.
    """
    orch = Orchestrator(wiki_root=wiki_root, model="test")
    assert orch.wiki_root == wiki_root.resolve()


def test_wiki_root_propagates_to_layout(wiki_root: Path) -> None:
    """The WikiLayout and the top-level wiki_root must agree."""
    orch = Orchestrator(wiki_root=wiki_root, model="test")
    assert orch.layout.root == orch.wiki_root


def test_wiki_root_propagates_to_system_prompt(wiki_root: Path) -> None:
    """The agent's system prompt must include the resolved wiki root path.

    Sub-agents and tool calls rely on this for path scoping and
    path-aware reasoning.
    """
    orch = Orchestrator(wiki_root=wiki_root, model="test")
    prompt = orch._agent._system_prompts[0]  # type: ignore[attr-defined]
    assert str(orch.wiki_root) in prompt
    assert "Wiki root:" in prompt


def test_wiki_root_propagates_to_file_system_capability(wiki_root: Path) -> None:
    """The file_system capability must be scoped to the wiki root.

    This is the security boundary that prevents the agent from
    reading or writing outside the wiki.
    """
    orch = Orchestrator(wiki_root=wiki_root, model="test")

    # pydantic-ai-harness stores the per-agent capabilities under
    # `agent.root_capability` (a CombinedCapability with a `capabilities`
    # list). Find the FileSystem among them.
    root_cap = orch._agent.root_capability  # type: ignore[attr-defined]
    caps = getattr(root_cap, "capabilities", [])
    fs_caps = [
        c
        for c in caps
        if getattr(c, "__class__", type(c)).__name__ == "FileSystem"
    ]
    assert fs_caps, "expected a FileSystem capability in the orchestrator"
    # FileSystem stores the root under various names depending on the
    # harness version; check the obvious ones.
    fs = fs_caps[0]
    root = (
        getattr(fs, "root", None)
        or getattr(fs, "root_dir", None)
        or getattr(fs, "wiki_root", None)
    )
    assert root == orch.wiki_root, (
        f"file_system capability root ({root!r}) does not match "
        f"orchestrator wiki_root ({orch.wiki_root!r})"
    )


def test_wiki_root_resolution_handles_relative_paths(tmp_path: Path) -> None:
    """A relative `wiki_root` must be resolved to an absolute path.

    The CLI passes the `--wiki-root` option through Typer; a relative
    path is a common input. The orchestrator must canonicalize it
    once at construction so downstream components see a stable root.
    """
    import os

    cwd = tmp_path
    (cwd / "raw").mkdir()
    (cwd / "wiki").mkdir()
    (cwd / ".lies").mkdir()
    rel = Path("subdir-of-cwd")
    (cwd / rel).mkdir()
    (cwd / rel / "raw").mkdir()
    (cwd / rel / "wiki").mkdir()
    (cwd / rel / ".lies").mkdir()

    # Run from tmp_path so the relative path resolves against it
    old_cwd = os.getcwd()
    try:
        os.chdir(cwd)
        orch = Orchestrator(wiki_root=rel, model="test")
    finally:
        os.chdir(old_cwd)

    assert orch.wiki_root.is_absolute()
    assert orch.wiki_root == (cwd / rel).resolve()


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
def git_wiki(wiki_root: Path) -> Path:
    """A wiki_root initialised as a git repository with one initial commit.

    The orchestrator's host-side snapshot/rollback uses ``git stash`` to
    capture pre-ingest state, so the wiki must be a real git working tree.
    """
    (wiki_root / "wiki").mkdir(exist_ok=True)
    (wiki_root / "wiki" / "index.md").write_text("# Index\n")
    (wiki_root / "wiki" / "log.md").write_text("# Log\n")
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(wiki_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=wiki_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=wiki_root,
        check=True,
        capture_output=True,
    )
    (wiki_root / "initial.txt").write_text("init")
    subprocess.run(
        ["git", "add", "."], cwd=wiki_root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=wiki_root,
        check=True,
        capture_output=True,
    )
    return wiki_root


def _log_lines(repo: Path) -> list[str]:
    """Return the current commit log (one line per commit, oldest first)."""
    result = subprocess.run(
        ["git", "log", "--pretty=%H %s"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def _working_tree_files(repo: Path) -> dict[str, str | None]:
    """Return a {relpath: content} snapshot of the working tree (None = absent)."""
    result = subprocess.run(
        ["git", "ls-files", "-o", "--exclude-standard"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    files: dict[str, str | None] = {}
    for line in result.stdout.splitlines():
        if line:
            files[line] = None
    # Also include tracked files (modifications, deletions).
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    for line in status.stdout.splitlines():
        if not line:
            continue
        # Format: "XY path" (two status chars, space, path)
        path = line[3:].strip()
        # Strip leading rename arrows.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files[path] = None
    return files


def test_run_ingest_commits_atomically_on_success(git_wiki: Path) -> None:
    """A successful run_ingest leaves exactly one new commit on the wiki."""
    orch = Orchestrator(wiki_root=git_wiki, model="test")

    pre_log = _log_lines(git_wiki)

    # Simulate the agent writing a wiki page mid-ingest. TestModel returns
    # a plain text response without invoking any tools, so we patch the
    # agent's run_sync to also drop a file into the working tree.
    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        # Drop a new wiki page as the agent "would" have done.
        (git_wiki / "wiki" / "new-entity.md").parent.mkdir(
            parents=True, exist_ok=True
        )
        (git_wiki / "wiki" / "new-entity.md").write_text("# New Entity\n")
        return mock.Mock(output="ingested ok")

    with mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync):
        result = orch.run_ingest("raw/some-source.md")

    assert result == "ingested ok"
    post_log = _log_lines(git_wiki)
    # Exactly one new commit was added.
    assert len(post_log) == len(pre_log) + 1
    new_commit = post_log[0]  # git log prints newest-first
    assert new_commit != pre_log[0]  # newest commit is different
    # The new commit's message matches the ingest convention.
    sha, _, msg = new_commit.partition(" ")
    assert msg.startswith(("ingest:", "ingest "))
    # The new entity file is in the commit.
    show = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", sha],
        cwd=git_wiki,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "wiki/new-entity.md" in show.stdout
    # No leftover stash entries -- the snapshot was discarded on success.
    stash = subprocess.run(
        ["git", "stash", "list"],
        cwd=git_wiki,
        capture_output=True,
        text=True,
        check=True,
    )
    assert stash.stdout.strip() == ""


def test_run_ingest_rolls_back_on_agent_exception(git_wiki: Path) -> None:
    """If the agent raises mid-ingest, the wiki working tree is restored.

    This is the headline test for Finding 3. The agent is injected with a
    mid-ingest failure: it writes a partial page, then raises. After
    ``run_ingest`` propagates the exception, the wiki must be byte-identical
    to its pre-ingest state (no committed changes, no uncommitted changes,
    no leftover stash entries).
    """
    orch = Orchestrator(wiki_root=git_wiki, model="test")
    pre_log = _log_lines(git_wiki)
    pre_files = _working_tree_files(git_wiki)

    class IngestFailure(RuntimeError):
        """Simulates a sub-agent crashing partway through the ingest."""

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        # Drop a partial page as the agent "would" have done before crashing.
        (git_wiki / "wiki" / "partial-page.md").write_text(
            "# Partial page (would crash)\n"
        )
        raise IngestFailure("source-reader hit a malformed URL")

    with (
        mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync),
        pytest.raises(IngestFailure, match="malformed URL"),
    ):
        orch.run_ingest("raw/broken-source.md")

    # No new commit was created.
    post_log = _log_lines(git_wiki)
    assert post_log == pre_log
    # No uncommitted files left behind.
    post_files = _working_tree_files(git_wiki)
    assert post_files == pre_files
    # The partial file the agent wrote is gone.
    assert not (git_wiki / "wiki" / "partial-page.md").exists()
    # No leftover stash entries.
    stash = subprocess.run(
        ["git", "stash", "list"],
        cwd=git_wiki,
        capture_output=True,
        text=True,
        check=True,
    )
    assert stash.stdout.strip() == ""


def test_run_ingest_rolls_back_on_keyboard_interrupt(git_wiki: Path) -> None:
    """A ``KeyboardInterrupt`` during the agent call is also rolled back.

    ``run_ingest`` uses ``except BaseException`` so Ctrl-C is treated as
    a failure and the working tree is restored. This is the safety
    contract for a long-running REPL ingest: a user interrupt cannot
    leave the wiki half-modified.
    """
    orch = Orchestrator(wiki_root=git_wiki, model="test")
    pre_log = _log_lines(git_wiki)

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        (git_wiki / "wiki" / "interrupted-page.md").write_text(
            "# Interrupted\n"
        )
        raise KeyboardInterrupt()

    with (
        mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync),
        pytest.raises(KeyboardInterrupt),
    ):
        orch.run_ingest("raw/source.md")

    # No new commits, no leftover file, no leftover stash.
    assert _log_lines(git_wiki) == pre_log
    assert not (git_wiki / "wiki" / "interrupted-page.md").exists()
    stash = subprocess.run(
        ["git", "stash", "list"],
        cwd=git_wiki,
        capture_output=True,
        text=True,
        check=True,
    )
    assert stash.stdout.strip() == ""


def test_run_ingest_rolls_back_when_commit_fails(git_wiki: Path) -> None:
    """If atomic_commit fails after the agent succeeds, the wiki is still
    rolled back.

    The agent's edits are not visible to the caller: no new commit, no
    uncommitted changes, no leftover stash.
    """
    orch = Orchestrator(wiki_root=git_wiki, model="test")
    pre_log = _log_lines(git_wiki)

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        (git_wiki / "wiki" / "orphan-page.md").write_text("# Orphan\n")
        return mock.Mock(output="agent done")

    # Patch atomic_commit so it raises CommitError on the post-agent step.
    # The import is local to Orchestrator.run_ingest, so we patch the
    # symbol in the orchestrator module's namespace.
    def boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise CommitError("simulated post-ingest commit failure")

    with (
        mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync),
        mock.patch("lies.orchestrator.atomic_commit", side_effect=boom),
        pytest.raises(CommitError, match="simulated post-ingest commit"),
    ):
        orch.run_ingest("raw/some-source.md")

    # The agent's edits are gone, no new commit, no leftover stash.
    assert _log_lines(git_wiki) == pre_log
    assert not (git_wiki / "wiki" / "orphan-page.md").exists()
    stash = subprocess.run(
        ["git", "stash", "list"],
        cwd=git_wiki,
        capture_output=True,
        text=True,
        check=True,
    )
    assert stash.stdout.strip() == ""


def test_run_ingest_preserves_pre_existing_dirty_state_on_failure(
    git_wiki: Path,
) -> None:
    """If the wiki was already dirty when ``run_ingest`` was called, that
    dirty state is preserved on the failure path.

    The rollback restores the working tree to *exactly* what it was before
    the call, including any uncommitted changes the user had.
    """
    # Pre-state: a tracked file with a user edit.
    (git_wiki / "initial.txt").write_text("user in-progress edit")
    (git_wiki / "user-notes.md").write_text("# WIP\n")

    orch = Orchestrator(wiki_root=git_wiki, model="test")
    pre_initial = (git_wiki / "initial.txt").read_text()
    pre_notes = (git_wiki / "user-notes.md").read_text()
    pre_log = _log_lines(git_wiki)

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        # The agent's "edits" -- a brand-new file and a tracked file change.
        (git_wiki / "wiki" / "agent-entity.md").write_text("# Agent\n")
        (git_wiki / "initial.txt").write_text("agent overwrote this")
        raise RuntimeError("agent crashed mid-ingest")

    with (
        mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync),
        pytest.raises(RuntimeError, match="agent crashed"),
    ):
        orch.run_ingest("raw/source.md")

    # The agent's edits are gone; the user's pre-existing dirty state is back.
    assert (git_wiki / "initial.txt").read_text() == pre_initial
    assert (git_wiki / "user-notes.md").read_text() == pre_notes
    assert not (git_wiki / "wiki" / "agent-entity.md").exists()
    # No new commits.
    assert _log_lines(git_wiki) == pre_log
    # No leftover stash.
    stash = subprocess.run(
        ["git", "stash", "list"],
        cwd=git_wiki,
        capture_output=True,
        text=True,
        check=True,
    )
    assert stash.stdout.strip() == ""


def test_run_ingest_success_discards_pre_existing_stash(
    git_wiki: Path,
) -> None:
    """On the success path, pre-existing dirty state is intentionally
    discarded: an ingest is an atomic, all-or-nothing operation.

    The snapshot's job is to enable rollback; once the ingest commits
    cleanly, the snapshot is no longer needed and is dropped. This is
    the documented contract: callers who want their dirty changes kept
    should commit them before calling ``run_ingest``.
    """
    (git_wiki / "user-notes.md").write_text("# WIP\n")
    orch = Orchestrator(wiki_root=git_wiki, model="test")

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        (git_wiki / "wiki" / "fresh-page.md").write_text("# Fresh\n")
        return mock.Mock(output="ok")

    with mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync):
        result = orch.run_ingest("raw/source.md")

    assert result == "ok"
    # The new page is committed; the user's WIP is gone.
    assert (git_wiki / "wiki" / "fresh-page.md").exists()
    assert not (git_wiki / "user-notes.md").exists()
    stash = subprocess.run(
        ["git", "stash", "list"],
        cwd=git_wiki,
        capture_output=True,
        text=True,
        check=True,
    )
    assert stash.stdout.strip() == ""


def test_run_ingest_on_clean_wiki_with_no_agent_writes(
    git_wiki: Path,
) -> None:
    """If the agent returns without writing any files, run_ingest still
    completes cleanly (no commit, no rollback) and propagates the agent's
    text output.

    This pins the "agent decided there was nothing to do" path. atomic_commit
    raises CommitError("nothing to commit") which is propagated -- the
    caller sees the error and the wiki is unchanged.
    """
    orch = Orchestrator(wiki_root=git_wiki, model="test")
    pre_log = _log_lines(git_wiki)

    def fake_run_sync(self, prompt: str):  # type: ignore[no-untyped-def]
        return mock.Mock(output="nothing to ingest")

    with (
        mock.patch.object(type(orch._agent), "run_sync", new=fake_run_sync),
        pytest.raises(CommitError, match="nothing to commit"),
    ):
        orch.run_ingest("raw/empty-source.md")

    # No new commit was created; the wiki is byte-identical.
    assert _log_lines(git_wiki) == pre_log
    stash = subprocess.run(
        ["git", "stash", "list"],
        cwd=git_wiki,
        capture_output=True,
        text=True,
        check=True,
    )
    assert stash.stdout.strip() == ""
