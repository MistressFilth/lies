"""End-to-end integration test using a fixture wiki and a mocked LLM.

This test exercises the full LIES flow on a real fixture wiki:
    1. Verify wiki layout is detected
    2. Verify schema loads
    3. Construct the orchestrator (mocked LLM)
    4. Run a lint command and verify it returns a string
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from lies.orchestrator import Orchestrator
from lies.qmd import qmd_update
from lies.schema import load_schema
from lies.wiki.layout import WikiLayout

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample-wiki"


@pytest.fixture
def wiki_copy(tmp_path: Path) -> Path:
    """Copy the fixture wiki to a tmp directory and init git there."""
    target = tmp_path / "wiki"
    shutil.copytree(FIXTURE, target)
    subprocess.run(["git", "init", "--initial-branch=main", str(target)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=target, check=True, capture_output=True)
    return target


def test_layout_resolves(wiki_copy: Path) -> None:
    layout = WikiLayout(wiki_copy)
    assert layout.is_git_repo() is True
    assert layout.index_path.exists()
    assert layout.log_path.exists()


def test_schema_loads(wiki_copy: Path) -> None:
    layout = WikiLayout(wiki_copy)
    schema = load_schema(layout)
    assert "Page types" in schema or "page types" in schema


def test_orchestrator_constructs(wiki_copy: Path) -> None:
    orch = Orchestrator(wiki_root=wiki_copy, model="test")
    assert orch is not None


def test_orchestrator_runs_lint(wiki_copy: Path) -> None:
    """The orchestrator's underlying agent is mocked via TestModel.

    TestModel is configured with `call_tools=[]` so it returns a plain text
    response instead of trying to invoke the orchestrator's many tools
    (delegate_task, run_workflow, run_code, etc.) -- which would loop on
    invalid workflow scripts.
    """
    orch = Orchestrator(wiki_root=wiki_copy, model="test")
    with orch._agent.override(model=TestModel(call_tools=[], custom_output_text="lint ok")):
        output = orch.run("lint")
    assert isinstance(output, str)


def test_qmd_update_raises_cleanly_when_not_installed(wiki_copy: Path) -> None:
    """If qmd is missing, qmd_update should raise QmdNotInstalledError, not crash."""
    from lies.qmd.cli import QmdNotInstalledError
    if shutil.which("qmd") is not None:
        pytest.skip("qmd is installed; skipping not-installed test")
    with pytest.raises(QmdNotInstalledError):
        qmd_update(wiki_copy)
