"""End-to-end smoke test: boot the FastMCP server in-process and
exercise the public surface through a real Client round-trip.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from fastmcp import Client

from lies.mcp.server import mcp
from lies.orchestrator import Orchestrator
from lies.query.models import SynthesizedAnswer
from lies.wiki.wiki import Wiki

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample-wiki"


@pytest.fixture
async def client() -> Client:
    async with Client(mcp) as c:
        yield c


@pytest.fixture
def sample_wiki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wiki:
    """Copy the sample fixture wiki to the XDG data root and return its
    ``Wiki`` handle.

    The MCP server resolves wikis by name through ``Wiki.require``, which
    checks ``Wiki.data_root_for(name).exists()`` under the autouse
    XDG isolation. So the wiki must live at
    ``$XDG_DATA_HOME/lies/<name>/``.
    """
    name = "sample"
    target = Wiki.data_root_for(name)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, target, dirs_exist_ok=True)
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
    subprocess.run(["git", "add", "."], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=target, check=True, capture_output=True)
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    return Wiki.require(name)


async def test_query_tool_round_trip(
    client: Client,
    sample_wiki: Wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real ``call_tool`` to ``query`` returns a structured result."""
    fake_answer = SynthesizedAnswer(
        answer="### What is MVCC?\n\nA concurrency protocol.",
        fallback_used=False,
        fallback_reason="",
    )

    # ``Orchestrator.run_query`` is a deterministic, extractive wrapper over
    # ``synthesize_answer`` (no agent call, no LLM). Mocking it directly is
    # the real-Orchestrator pattern here: there is no ``_agent.run_sync``
    # to mock because ``run_query`` never goes through the agent. We also
    # no-op ``Orchestrator._build`` so the constructor doesn't try to spin
    # up the qmd stdio MCP transport in the test process.
    def fake_run_query(self, question: str) -> SynthesizedAnswer:
        return fake_answer

    with (
        mock.patch.object(Orchestrator, "_build", lambda self: None),
        mock.patch.object(Orchestrator, "run_query", new=fake_run_query),
    ):
        result = await client.call_tool(
            "query",
            {"question": "What is MVCC?", "name": "sample"},
        )

    # FastMCP returns a CallResult with .structured_content for pydantic outputs.
    payload = result.structured_content or {}
    assert payload.get("answer", "").startswith("### What is MVCC?")
    assert payload.get("fallback_used") is False
