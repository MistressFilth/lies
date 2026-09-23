from dataclasses import is_dataclass
from pathlib import Path

from lies.agents.page_writer import PageDiff, PageOperation


def test_page_diff_is_dataclass() -> None:
    assert is_dataclass(PageDiff)


def test_page_diff_constructs_for_create() -> None:
    d = PageDiff(
        path=Path("wiki/x/concepts/foo.md"),
        operation=PageOperation.CREATE,
        new_content="# foo",
    )
    assert d.old_content is None
    assert d.new_content == "# foo"


def test_page_writer_agent_accepts_dataclass_list_output() -> None:
    # Regression pin: previously locked by BaseModel assumption.
    # NOTE: brief shows verbatim `page_writer_agent()` which requires
    # ANTHROPIC_API_KEY (the default model is `anthropic:claude-opus-4-7`).
    # CI does not set ANTHROPIC_API_KEY, so we pass TestModel() to keep the
    # construction assertion hermetic — the point of this pin is that the
    # agent *constructs* with dataclass deps/output, not that it talks to
    # Anthropic. Pattern matches tests/unit/agents/test_collection_author_dataclass_pin.py.
    from pydantic_ai.models.test import TestModel

    from lies.agents.page_writer import page_writer_agent

    agent = page_writer_agent(model=TestModel())
    assert agent is not None
