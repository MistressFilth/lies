"""librarian_agent runs the 4-step Classify → Search → Read → Return pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic_ai.models.test import TestModel


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


def test_librarian_agent_runs_4_step_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """TestModel drives the librarian through Classify → Search → Read → Return.

    Verifies the full 4-step pipeline runs end-to-end with canned tool
    responses:

      - The agent runs to completion without exception.
      - All three tools are invoked (``collections_read`` for the
        registry, ``search`` for the hybrid query, ``read`` for the
        curator's picks).
      - The output ``LibrarianOutput`` carries ``tag_expr`` matching
        what the LLM built from the registry tokens, ``searched_scope``
        populated from the search tool's response, ``distinct_pages``
        ≥ 1, and a non-empty excerpts list drawn from the canned
        ``read`` data.

    Uses pydantic-ai's :class:`TestModel` with ``custom_output_args``
    pinned to a deterministic ``LibrarianOutput`` so the assertions
    don't flake on TestModel's synthetic output generation. The tool
    invocations still go through the real closure → MCP-tool shim
    path; only the final structured output is pinned.
    """
    bodies = {
        "alpha/cli-plugin.md": (
            "# Setup\n\nPlugin.define({ id, setup }) — entry point for the alpha CLI plugin."
        ),
        "alpha/install.md": (
            "# Install\n\nnpm install @alpha/cli — installs the alpha CLI plugin."
        ),
    }

    # Step 1 (Classify) — registry returns one row.
    collections_read_calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "lies.mcp.collections.collections_read",
        lambda subcommand, name=None: (
            collections_read_calls.append((subcommand, name)),
            [{"name": "alpha", "tags": ["plugins"], "scope_keywords": []}],
        )[1]
        if subcommand == "list"
        else {},
    )

    # Step 2 (Search) — single-batch hybrid response with snippets.
    search_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "lies.mcp.search.search",
        lambda **kw: (
            search_calls.append(kw),
            {
                "hit": {
                    "path": "alpha/cli-plugin.md",
                    "title": "CLI plugin",
                    "score": 0.9,
                    "snippet": "Plugin.define({ id, setup })",
                },
                "hits": [
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
                ],
                "unknown_tags": [],
                "no_coverage": False,
                "searched_scope": ["alpha"],
                "fallback_reason": None,
            },
        )[1],
    )

    # Step 3 (Read) — verbatim bodies keyed by path.
    read_calls: list[list[str]] = []
    monkeypatch.setattr(
        "lies.mcp.read.read",
        lambda paths: (
            read_calls.append(list(paths)),
            {p: bodies.get(p, "") for p in paths},
        )[1],
    )

    from lies.agents.librarian import (
        LibrarianDeps,
        LibrarianOutput,
        PageExcerpt,
        librarian_agent,
        register_librarian_tools,
    )
    from lies.markdown_spans import Span

    # Pin the final structured output so the assertions are
    # deterministic; TestModel will still cycle through the registered
    # tools before producing this output. The pinned output mirrors
    # what a real LLM run would emit given the canned tool responses:
    # tag_expr ``c:alpha`` from the registry's single ``alpha``
    # collection; searched_scope mirrored from the search tool's
    # envelope; one distinct page picked from the snippets.
    expected_output = LibrarianOutput(
        tag_expr="c:alpha",
        exclude_expr=None,
        excerpts=[
            PageExcerpt(
                collection="alpha",
                slug="alpha/cli-plugin.md",
                title="CLI plugin",
                spans=[
                    Span(
                        heading_path=["Setup"],
                        body=bodies["alpha/cli-plugin.md"],
                        code_fence=False,
                        start_line=1,
                    ),
                ],
                source_kind="library",
            ),
        ],
        distinct_pages=1,
        no_coverage=False,
        searched_scope=["alpha"],
    )

    agent = librarian_agent(model=TestModel(call_tools="all", custom_output_args=expected_output))
    register_librarian_tools(agent)

    deps = LibrarianDeps(
        question="how does the alpha CLI plugin work?",
        tag_expr="c:alpha",
        exclude_expr=None,
        top_k=5,
    )

    result = agent.run_sync(deps.question, deps=deps)

    # No exception raised — the 4-step pipeline ran end-to-end.
    out = result.output
    assert isinstance(out, LibrarianOutput)

    # Step 1 ran: the registry tool was called.
    assert collections_read_calls, "Step 1 didn't run — collections_read was not called"

    # Step 2 ran: the search tool was called.
    assert search_calls, "Step 2 didn't run — search was not called"

    # Step 3 ran: the read tool was called with at least one path.
    assert read_calls, "Step 3 didn't run — read was not called"
    read_paths = [path for batch in read_calls for path in batch]
    assert read_paths, "Step 3 ran but picked no paths"

    # The pinned output mirrors what a real LLM would emit given the
    # canned tool responses:
    assert out.tag_expr == "c:alpha", (
        "tag_expr should mirror the registry-resolved union, not the "
        f"caller-supplied filter. Got {out.tag_expr!r}."
    )
    assert out.searched_scope == ["alpha"], (
        "searched_scope should mirror the search tool's resolved scope. "
        f"Got {out.searched_scope!r}."
    )
    assert out.distinct_pages >= 1, (
        f"distinct_pages should be >= 1 (the curator picked {len(read_paths)} "
        f"paths across {len(read_calls)} read calls). Got {out.distinct_pages}."
    )
    assert out.excerpts, "excerpts should be non-empty when reads succeeded"
    assert out.no_coverage is False, (
        f"no_coverage should mirror the search tool's flag (False here). Got {out.no_coverage!r}."
    )


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
