from __future__ import annotations

from pydantic_ai.models.test import TestModel

from lies.agents.page_writer import (
    PageDiff,
    PageWriterDeps,
    _build_page_writer_prompt_for_test,
    page_writer_agent,
)


def test_page_writer_agent_exists() -> None:
    agent = page_writer_agent(model=TestModel())
    assert agent is not None


def test_page_writer_returns_diffs() -> None:
    agent = page_writer_agent(model=TestModel())
    with agent.override(model=TestModel()):
        result = agent.run_sync("Create a page for entity 'postgres'.")
    assert result is not None
    diffs = result.output
    assert isinstance(diffs, list)
    # TestModel returns a default-constructed list of PageDiff.
    for diff in diffs:
        assert isinstance(diff, PageDiff)


def test_page_writer_deps_renders_conventions() -> None:
    deps = PageWriterDeps(
        question="distill this into structured pages",
        schema_text="# LIES Schema\n\n## Page types\n- entity\n",
        existing_pages=[
            ("wiki/concepts/alpha.md", "introduces the alpha concept"),
        ],
    )
    prompt = _build_page_writer_prompt_for_test(deps)
    assert "wiki/" in prompt
    assert "wiki/<collection>/<file>" in prompt
    assert "wiki/concepts/alpha.md" in prompt
    assert "introduces the alpha concept" in prompt
    assert "schema_text" in prompt or "# LIES Schema" in prompt


def test_page_writer_prompt_without_deps_returns_base() -> None:
    base = _build_page_writer_prompt_for_test(None)
    assert isinstance(base, str)
    assert "page-writer" in base.lower() or "page_writer" in base.lower()
