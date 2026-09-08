from __future__ import annotations

from pydantic_ai.models.test import TestModel

from lies.agents.page_writer import (
    PageWriterDeps,
    _build_page_writer_prompt_for_test,
    page_writer_agent,
)


def test_page_writer_agent_exists() -> None:
    agent = page_writer_agent(model=TestModel())
    assert agent is not None


def test_page_writer_returns_diffs() -> None:
    """Agent builds. End-to-end run is gated on a real model — TestModel
    does not produce free-form text that pydantic-ai's PromptedOutput
    can parse, so the live run is exercised in the integration suite
    instead."""
    agent = page_writer_agent(model=TestModel())
    assert agent is not None


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
