from dataclasses import fields, is_dataclass

from lies.agents.source_reader import SourceExtraction


def test_source_extraction_is_dataclass() -> None:
    assert is_dataclass(SourceExtraction)


def test_source_extraction_defaults_construct_empty() -> None:
    s = SourceExtraction()
    assert s.claims == []
    assert s.entities == []
    assert s.concepts == []
    assert s.comparisons == []
    assert s.summary == ""


def test_source_extraction_independent_instances() -> None:
    # Regression pin for the mutable-default footgun.
    a = SourceExtraction()
    b = SourceExtraction()
    a.claims.append("x")
    assert b.claims == []


def test_source_reader_agent_accepts_prompted_dataclass_output() -> None:
    # Regression pin: previously locked by BaseModel assumption.
    # NOTE: brief shows verbatim `source_reader_agent()` which requires
    # ANTHROPIC_API_KEY (the default model is `anthropic:claude-opus-4-7`).
    # CI does not set ANTHROPIC_API_KEY, so we pass TestModel() to keep the
    # construction assertion hermetic — the point of this pin is that the
    # agent *constructs* with a dataclass output_type, not that it talks to
    # Anthropic. Pattern matches tests/unit/agents/test_query_synthesizer_dataclass_pin.py
    # and tests/unit/agents/test_collection_author_dataclass_pin.py.
    from pydantic_ai.models.test import TestModel

    from lies.agents.source_reader import source_reader_agent

    agent = source_reader_agent(model=TestModel())
    assert agent is not None
    # Output type annotation still resolves.
    assert agent._output_type is not None  # type: ignore[attr-defined]


# Silence "imported but unused" — `fields` is part of the brief's verbatim
# dataclass-detection surface and stays imported for symmetry with the
# other pin tests' introspection imports.
_ = fields
