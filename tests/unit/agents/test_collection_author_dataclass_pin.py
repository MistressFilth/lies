from dataclasses import is_dataclass

from lies.agents.collection_author import (
    AuthorQuestion,
    CollectionAuthorDeps,
)


def test_author_question_is_dataclass() -> None:
    assert is_dataclass(AuthorQuestion)
    assert not hasattr(AuthorQuestion, "model_dump")  # BaseModel surface gone


def test_collection_author_deps_is_dataclass() -> None:
    assert is_dataclass(CollectionAuthorDeps)
    a = CollectionAuthorDeps(manifest=[{"path": "x.md"}])
    assert a.manifest == [{"path": "x.md"}]


def test_author_question_constructs_with_minimum_fields() -> None:
    q = AuthorQuestion(id="q1", prompt="what?")
    assert q.id == "q1"
    assert q.options is None
    assert q.default is None


def test_agent_construction_accepts_dataclass_deps_and_output() -> None:
    # Regression pin: previously locked by BaseModel assumption.
    # NOTE: brief shows verbatim `collection_author_agent()` which requires
    # ANTHROPIC_API_KEY (the default model is `anthropic:claude-opus-4-7`).
    # CI does not set ANTHROPIC_API_KEY, so we pass TestModel() to keep the
    # construction assertion hermetic — the point of this pin is that the
    # agent *constructs* with dataclass deps/output, not that it talks to
    # Anthropic. Pattern matches tests/unit/agents/test_collection_author.py.
    from pydantic_ai.models.test import TestModel

    from lies.agents.collection_author import collection_author_agent

    agent = collection_author_agent(model=TestModel())
    assert agent is not None
    # Output type annotation still resolves.
    assert agent._output_type is not None  # type: ignore[attr-defined]
