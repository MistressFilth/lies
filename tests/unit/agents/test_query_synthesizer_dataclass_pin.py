from dataclasses import is_dataclass

from lies.agents.query_synthesizer import (
    QUERY_SYNTHESIZER_SYSTEM_PROMPT,
    QueryAnswer,
)


def test_query_answer_is_dataclass() -> None:
    assert is_dataclass(QueryAnswer)


def test_query_answer_constructs() -> None:
    a = QueryAnswer(
        answer="see [link](wiki/x.md)",
        citations=["wiki/x.md"],
        should_file=False,
    )
    assert a.should_file is False
    assert a.citations == ["wiki/x.md"]


def test_query_answer_defaults_format_hint_md() -> None:
    """``format_hint`` defaults to "md" so existing callers compile."""
    qa = QueryAnswer(answer="x", citations=[], should_file=False)
    assert qa.format_hint == "md"


def test_query_answer_explicit_format_hint() -> None:
    """``format_hint`` accepts the three documented values."""
    for hint in ("md", "table", "marp"):
        qa = QueryAnswer(answer="x", citations=[], should_file=False, format_hint=hint)
        assert qa.format_hint == hint


def test_query_answer_format_hint_default_is_md() -> None:
    """Regression pin: default matches spec § 13."""
    assert QueryAnswer(answer="x", citations=[], should_file=False).format_hint == "md"


def test_query_synthesizer_prompt_includes_format_taxonomy() -> None:
    """The system prompt teaches the format_hint taxonomy."""
    assert "format_hint" in QUERY_SYNTHESIZER_SYSTEM_PROMPT
    assert "table" in QUERY_SYNTHESIZER_SYSTEM_PROMPT
    assert "marp" in QUERY_SYNTHESIZER_SYSTEM_PROMPT
    # Hybrid taxonomy: instructions AND shape examples both present.
    assert (
        "Pick the format" in QUERY_SYNTHESIZER_SYSTEM_PROMPT
        or "pick the format" in QUERY_SYNTHESIZER_SYSTEM_PROMPT.lower()
    )


def test_query_synthesizer_agent_accepts_dataclass_output() -> None:
    # Regression pin: previously locked by BaseModel assumption.
    # NOTE: brief shows verbatim `query_synthesizer_agent()` which requires
    # ANTHROPIC_API_KEY (the default model is `anthropic:claude-opus-4-7`).
    # CI does not set ANTHROPIC_API_KEY, so we pass TestModel() to keep the
    # construction assertion hermetic — the point of this pin is that the
    # agent *constructs* with dataclass deps/output, not that it talks to
    # Anthropic. Pattern matches tests/unit/agents/test_collection_author_dataclass_pin.py.
    from pydantic_ai.models.test import TestModel

    from lies.agents.query_synthesizer import query_synthesizer_agent

    agent = query_synthesizer_agent(model=TestModel())
    assert agent is not None
