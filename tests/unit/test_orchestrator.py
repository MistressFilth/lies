from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from lies.orchestrator import Orchestrator


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
