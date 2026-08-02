"""End-to-end smoke test: boot the FastMCP server in-process and
exercise the public surface through a real Client round-trip.
"""

from __future__ import annotations

from unittest import mock

import pytest
from fastmcp import Client

from lies.mcp.server import mcp
from lies.orchestrator import Orchestrator
from lies.query.models import SynthesizedAnswer


@pytest.fixture
async def client() -> Client:
    async with Client(mcp) as c:
        yield c


async def test_query_tool_round_trip(
    client: Client,
    sample_wiki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real ``call_tool`` to ``query`` returns a structured result."""
    monkeypatch.setenv("LIES_WIKI_ROOT", str(sample_wiki.root))

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
            {"question": "What is MVCC?", "wiki_root": str(sample_wiki.root)},
        )

    # FastMCP returns a CallResult with .structured_content for pydantic outputs.
    payload = result.structured_content or {}
    assert payload.get("answer", "").startswith("### What is MVCC?")
    assert payload.get("fallback_used") is False
