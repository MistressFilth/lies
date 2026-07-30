# tests/unit/memory/test_tools.py
from pathlib import Path
from textwrap import dedent

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.test import TestModel

from lies.memory.service import WikiMemoryService
from lies.memory.tools import WikiMemoryDeps, register_read_tools
from lies.wiki.layout import WikiLayout


@pytest.fixture
def wiki(tmp_path: Path) -> WikiLayout:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "concepts" / "x.md").write_text(
        dedent(
            """\
            ---
            title: X
            type: concept
            ---
            # X

            The thing.
            """
        ),
        encoding="utf-8",
    )
    (root / "wiki" / "index.md").write_text("- [X](concepts/x.md)\n", encoding="utf-8")
    return WikiLayout(root)


def _registered_tool_names(agent: Agent[WikiMemoryDeps, object]) -> list[str]:
    """Return the names of tools registered on ``agent``.

    pydantic_ai exposes the function toolset via ``_function_toolset``;
    iterating ``agent.toolsets`` is the public route but each toolset's
    ``tools`` mapping yields the registered tool names.
    """
    names: list[str] = []
    for toolset in agent.toolsets:
        names.extend(toolset.tools.keys())
    return sorted(set(names))


def test_register_read_tools_attaches_two_tools(wiki: WikiLayout) -> None:
    agent: Agent[WikiMemoryDeps, object] = Agent(TestModel(), deps_type=WikiMemoryDeps)
    register_read_tools(agent)
    tool_names = _registered_tool_names(agent)
    assert "wiki_search" in tool_names
    assert "wiki_read" in tool_names


def test_wiki_search_tool_returns_evidence(wiki: WikiLayout) -> None:
    from lies.memory.tools import wiki_search_tool

    service = WikiMemoryService(wiki)
    deps = WikiMemoryDeps(layout=wiki, service=service)
    ctx = _build_ctx(deps)
    result = wiki_search_tool(ctx, "X", 5)
    assert result["pages"]


def test_wiki_read_tool_rejects_unknown_id(wiki: WikiLayout) -> None:
    from lies.memory.models import WikiPageNotFound
    from lies.memory.tools import wiki_read_tool

    service = WikiMemoryService(wiki)
    deps = WikiMemoryDeps(layout=wiki, service=service)
    ctx = _build_ctx(deps)
    with pytest.raises(WikiPageNotFound):
        wiki_read_tool(ctx, ["not-a-real-id"])


def _build_ctx(deps: WikiMemoryDeps) -> RunContext[WikiMemoryDeps]:
    model = TestModel()
    return RunContext(
        model=model,
        deps=deps,
        usage=None,  # type: ignore[arg-type]
    )
