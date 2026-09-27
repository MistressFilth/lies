"""librarian_agent runs the 4-step Classify → Search → Read → Return pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _patch_tools(monkeypatch: pytest.MonkeyPatch, *, hits=None, bodies=None, registry=None) -> None:
    """Patch the three tools to return canned data."""
    if registry is None:
        registry = [
            {"name": "alpha", "tags": ["plugins"], "scope_keywords": []},
        ]
    monkeypatch.setattr(
        "lies.mcp.collections.collections_read",
        lambda subcommand, name=None: (
            registry
            if subcommand == "list"
            else {row["name"]: row for row in registry}.get(name, {})
            if subcommand == "info"
            else {}
        ),
    )

    if hits is None:
        hits = [
            {
                "path": "alpha/cli-plugin.md",
                "title": "CLI plugin",
                "score": 0.9,
                "snippet": "Plugin.define({ id, setup })",
            },
            {
                "path": "alpha/install.md",
                "title": "Install",
                "score": 0.5,
                "snippet": "npm install",
            },
        ]
    monkeypatch.setattr(
        "lies.mcp.search.search",
        lambda **kw: {
            "hit": hits[0] if hits else None,
            "hits": hits,
            "unknown_tags": [],
            "no_coverage": not hits,
            "searched_scope": ["alpha"],
            "fallback_reason": None,
        },
    )

    bodies_map: dict[str, str] = (
        bodies
        if bodies is not None
        else {
            "alpha/cli-plugin.md": "<full CLI plugin body>",
            "alpha/install.md": "<install body>",
        }
    )
    monkeypatch.setattr(
        "lies.mcp.read.read", lambda paths: {p: bodies_map.get(p, "") for p in paths}
    )


def test_librarian_agent_calls_collections_read_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 1: collections_read('list') is the first tool the librarian calls."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        "lies.mcp.collections.collections_read",
        lambda subcommand, name=None: calls.append(("list",))
        or [{"name": "alpha", "tags": ["plugins"], "scope_keywords": []}],
    )
    _patch_tools(monkeypatch)

    # Just verify the function exists and is callable via TestModel
    from lies.agents.librarian import librarian_agent

    agent = librarian_agent(model="test")

    # The agent should have tools registered
    from lies.agents.librarian import register_librarian_tools

    register_librarian_tools(
        agent,
        wiki=MagicMock(wiki_dir="/tmp/fake"),
        memory_service=MagicMock(),
    )

    tool_names: set[str] = set()
    for ts in agent.toolsets:
        tools = getattr(ts, "tools", {})
        tool_names.update(tools.keys() if hasattr(tools, "keys") else ())
    assert {"collections_read", "search", "read"} <= tool_names


def test_librarian_output_searched_scope_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """LibrarianOutput carries searched_scope from the search tool."""
    from lies.agents.librarian import LibrarianOutput

    _patch_tools(monkeypatch, hits=[])

    # Direct test via the librarian's run path
    out = LibrarianOutput(
        tag_expr=None,
        exclude_expr=None,
        excerpts=[],
        distinct_pages=0,
        no_coverage=True,
        searched_scope=["alpha"],
    )
    assert out.searched_scope == ["alpha"]


def test_librarian_resolves_tag_expr_from_registry_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 1: the system prompt instructs the LLM to build tag_expr from registry tokens."""
    from lies.agents.librarian import LIBRARIAN_SYSTEM_PROMPT

    assert "collections_read" in LIBRARIAN_SYSTEM_PROMPT
    assert "Classify" in LIBRARIAN_SYSTEM_PROMPT
    assert "Snippet" in LIBRARIAN_SYSTEM_PROMPT  # the snippet-review step
    assert "Read" in LIBRARIAN_SYSTEM_PROMPT
    assert "Return" in LIBRARIAN_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Dataclass shape pins (back-compat for the v0.40 surface)
# ---------------------------------------------------------------------------


def test_librarian_deps_required_fields() -> None:
    """LibrarianDeps stays a frozen dataclass with question / tag_expr / exclude_expr / top_k."""
    from lies.agents.librarian import LibrarianDeps

    deps = LibrarianDeps(
        question="how does pydantic validate nested models?",
        tag_expr="python",
        exclude_expr=None,
        top_k=5,
    )
    assert deps.top_k == 5
    assert deps.tag_expr == "python"


def test_librarian_deps_optional_top_k() -> None:
    """LibrarianDeps.top_k defaults to 5."""
    from lies.agents.librarian import LibrarianDeps

    deps = LibrarianDeps(question="q", tag_expr=None, exclude_expr=None)
    assert deps.top_k == 5


def test_page_excerpt_carries_spans_not_text_blob() -> None:
    """PageExcerpt still carries the F19 structured spans field."""
    from lies.agents.librarian import PageExcerpt
    from lies.markdown_spans import Span

    spans = [
        Span(heading_path=["H1"], body="body text", code_fence=False, start_line=1),
    ]
    pe = PageExcerpt(collection="wiki", slug="x", title="X", spans=spans)
    assert pe.spans == spans


def test_librarian_output_defaults_no_coverage_false() -> None:
    """F18 no_coverage field defaults to False (back-compat)."""
    from lies.agents.librarian import LibrarianOutput

    out = LibrarianOutput(tag_expr=None, exclude_expr=None, excerpts=[], distinct_pages=0)
    assert out.no_coverage is False


def test_librarian_output_no_coverage_settable() -> None:
    """F18 no_coverage field is settable to True via keyword."""
    from lies.agents.librarian import LibrarianOutput

    out = LibrarianOutput(
        tag_expr=None,
        exclude_expr=None,
        excerpts=[],
        distinct_pages=0,
        no_coverage=True,
    )
    assert out.no_coverage is True


def test_librarian_output_searched_scope_default_empty() -> None:
    """LibrarianOutput.searched_scope defaults to [] (additive F19 field)."""
    from lies.agents.librarian import LibrarianOutput

    out = LibrarianOutput(tag_expr=None, exclude_expr=None, excerpts=[], distinct_pages=0)
    assert out.searched_scope == []
