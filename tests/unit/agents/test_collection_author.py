"""Unit tests for the CollectionAuthorAgent structured output.

The agent drives a one-question-at-a-time conversation and returns either
an ``AuthorQuestion`` (requesting the next answer) or an ``AuthorProposal``
(serialized ``Collection`` record + rationale). The agent never touches
the filesystem directly.
"""

from __future__ import annotations

from pydantic_ai.models.test import TestModel

from lies.agents.collection_author import (
    AuthorOutput,
    AuthorProposal,
    AuthorQuestion,
    CollectionAuthorDeps,
    collection_author_agent,
)


def test_agent_emits_question_first() -> None:
    """First run produces one of the two AuthorOutput variants."""
    agent = collection_author_agent(model=TestModel())
    deps = CollectionAuthorDeps(manifest=[])
    out = agent.run_sync("add docs", deps=deps).output
    assert isinstance(out, (AuthorQuestion, AuthorProposal))


def test_proposal_serializes_collection() -> None:
    """An AuthorProposal can be dumped and has a 'collection' key."""
    proposal = AuthorProposal(
        collection={"name": "demo", "source": "https://example.com"},
        rationale="test",
    )
    payload = proposal.model_dump()
    assert "collection" in payload
    assert payload["collection"]["name"] == "demo"
    assert payload["rationale"] == "test"


def test_question_carries_id_and_prompt() -> None:
    """An AuthorQuestion has id, prompt, optional options/default."""
    question = AuthorQuestion(
        id="name",
        prompt="What name should the collection use?",
        options=["alpha", "beta"],
        default="alpha",
    )
    payload = question.model_dump()
    assert payload["id"] == "name"
    assert payload["prompt"] == "What name should the collection use?"
    assert payload["options"] == ["alpha", "beta"]
    assert payload["default"] == "alpha"


def test_deps_carry_manifest() -> None:
    """CollectionAuthorDeps carries the manifest list."""
    deps = CollectionAuthorDeps(
        manifest=[{"path": "raw/article.md", "format": "markdown"}],
    )
    assert deps.manifest[0]["path"] == "raw/article.md"


def test_agent_factory_returns_agent() -> None:
    """collection_author_agent() returns a non-None Agent with the right types."""
    agent = collection_author_agent(model=TestModel())
    assert agent is not None
    assert agent.output_type is AuthorOutput


def test_agent_runs_with_test_model() -> None:
    """End-to-end run via TestModel returns a valid AuthorOutput."""
    agent = collection_author_agent(model=TestModel())
    deps = CollectionAuthorDeps(manifest=[])
    result = agent.run_sync("add docs", deps=deps)
    out = result.output
    assert isinstance(out, (AuthorQuestion, AuthorProposal))
    # AuthorQuestion and AuthorProposal both have model_dump; verify dispatch.
    assert hasattr(out, "model_dump")
    payload = out.model_dump()
    assert isinstance(payload, dict)
