# tests/unit/memory/test_tools.py
from pathlib import Path
from textwrap import dedent

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.test import TestModel

from lies.memory.service import WikiMemoryService
from lies.memory.tools import WikiMemoryDeps, register_read_tools
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki


@pytest.fixture
def wiki(tmp_path: Path) -> Wiki:
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    wiki_obj = make_wiki(name="tools", data_root=root)
    (wiki_obj.wiki_dir / "concepts").mkdir(parents=True)
    (wiki_obj.wiki_dir / "concepts" / "x.md").write_text(
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
    (wiki_obj.wiki_dir / "index.md").write_text("- [X](concepts/x.md)\n", encoding="utf-8")
    return wiki_obj


def _registered_tool_names(agent: Agent[WikiMemoryDeps, object]) -> list[str]:
    """Return the names of tools registered on ``agent``.

    pydantic-ai exposes the function toolset via ``_function_toolset``;
    iterating ``agent.toolsets`` is the public route but each toolset's
    ``tools`` mapping yields the registered tool names.
    """
    names: list[str] = []
    for toolset in agent.toolsets:
        names.extend(toolset.tools.keys())
    return sorted(set(names))


def test_register_read_tools_attaches_two_tools(wiki: Wiki) -> None:
    agent: Agent[WikiMemoryDeps, object] = Agent(TestModel(), deps_type=WikiMemoryDeps)
    register_read_tools(agent)
    tool_names = _registered_tool_names(agent)
    assert "wiki_search" in tool_names
    assert "wiki_read" in tool_names


def test_wiki_search_tool_returns_evidence(wiki: Wiki, monkeypatch: pytest.MonkeyPatch) -> None:
    from lies.memory.tools import wiki_search_tool

    monkeypatch.setattr(
        "lies.qmd.cli.qmd_query",
        lambda *a, **kw: [{"path": "concepts/x.md", "score": 1.0}],
    )
    service = WikiMemoryService(wiki=wiki)
    deps = WikiMemoryDeps(wiki=wiki, service=service)
    ctx = _build_ctx(deps)
    result = wiki_search_tool(ctx, "X", 5)
    assert result["pages"]


def test_wiki_read_tool_rejects_unknown_id(wiki: Wiki) -> None:
    from lies.memory.models import WikiPageNotFound
    from lies.memory.tools import wiki_read_tool

    service = WikiMemoryService(wiki=wiki)
    deps = WikiMemoryDeps(wiki=wiki, service=service)
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
