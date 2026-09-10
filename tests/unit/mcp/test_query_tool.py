"""MCP `query` tool kwargs (Bundle C / F15) — tag_expr + exclude_tags.

The ``query`` MCP tool mirrors the explicit CLI form
(``--tag-expr`` / ``--exclude-tag``). The MCP layer parses + resolves
the filter before handing a ``ResolvedTagFilter`` to
``Orchestrator.run_query`` and converts ``TagExprUnknown`` to a typed
``ToolError`` so LLM callers see a useful message instead of a stack
trace.

Additive: existing callers (no ``tag_expr``) get the unfiltered
behavior — ``tag_filter=None`` flows straight to the orchestrator.

Tests build a real on-disk wiki with two tagged collections
(airflow, amazon) so ``_collect_available_tags`` reads real YAML, then
patch ``Orchestrator.run_query`` so the test does not depend on qmd
or the LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from lies.collections.record import Collection, save_collection
from lies.query.models import SynthesizedAnswer
from lies.query.tag_expr import And, Include

_NOW = datetime(2026, 9, 9, tzinfo=UTC)

# name -> tags. The implicit self-tag means ``airflow`` resolves even
# though ``airflow`` is incidentally in the tag list.
_COLLECTIONS = {
    "airflow": ["airflow", "provider"],
    "amazon": ["amazon", "aws"],
}


@pytest.fixture
def fake_wiki_with_collections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real on-disk wiki with airflow + amazon collections seeded.

    Patches ``lies.mcp.server.resolve_wiki`` so the MCP tool resolves
    to this wiki without touching ``LIES_XDG_DATA_HOME``. Tests still
    patch ``Orchestrator.run_query`` so no real retrieval runs.
    """
    from lies.mcp import server
    from lies.wiki.wiki import Wiki

    config_root = tmp_path / "config"
    collections_dir = config_root / "collections"
    collections_dir.mkdir(parents=True, exist_ok=True)

    wiki = Wiki(
        name="t",
        data_root=tmp_path / "data",
        config_root=config_root,
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )
    for coll_name, tags in _COLLECTIONS.items():
        save_collection(
            wiki,
            Collection(
                name=coll_name,
                path=tmp_path / coll_name,
                source=f"https://example.com/{coll_name}",
                tags=list(tags),
                scraper_cmd=None,
                doc_path=None,
                mapper_model=None,
                language="en",
                version="1.0.0",
                created_at=_NOW,
                updated_at=_NOW,
                config={},
            ),
        )

    monkeypatch.setattr(server, "resolve_wiki", lambda _name=None: wiki)
    return wiki


def _answer(**kwargs) -> SynthesizedAnswer:
    """Default-constructed SynthesizedAnswer with optional overrides."""
    return SynthesizedAnswer(
        answer="x",
        fallback_used=False,
        fallback_reason="",
        **kwargs,
    )


def test_mcp_query_tag_expr(monkeypatch, fake_wiki_with_collections) -> None:
    """``tag_expr='airflow&provider'`` parses + resolves to an And include.

    The MCP layer routes the include expression through
    ``parse`` + ``resolve`` against the wiki's available-tag set and
    hands ``Orchestrator.run_query`` a ``ResolvedTagFilter`` whose
    ``include`` is ``And(Include('airflow'), Include('provider'))``.
    """
    from lies.mcp import server

    fake = _answer()

    with mock.patch.object(server, "Orchestrator") as orch_cls:
        orch_cls.return_value.run_query.return_value = fake
        result = server.query(
            question="what is X?",
            tag_expr="airflow&provider",
        )

    call = orch_cls.return_value.run_query.call_args
    tag_filter = call.kwargs["tag_filter"]
    assert tag_filter is not None
    assert tag_filter.include == And(Include("airflow"), Include("provider"))
    assert tag_filter.exclude is None
    # No exception propagated; the fake SynthesizedAnswer came back.
    assert isinstance(result, server.SynthesizedMcpAnswer)


def test_mcp_query_exclude_tags_size_limit(monkeypatch, fake_wiki_with_collections) -> None:
    """``exclude_tags=['a', 'b']`` is rejected at the MCP boundary.

    The MCP surface mirrors the predecessor's ``mcp__plugin_ask_core__ask``
    shape (``exclude_tags`` is a list of tag names, size ≤ 1). The tool
    raises ``ToolError`` when more than one tag is supplied — the
    parser owns the grammar, but the MCP layer owns the size contract.
    """
    from fastmcp.exceptions import ToolError

    from lies.mcp import server

    with mock.patch.object(server, "Orchestrator") as orch_cls:
        with pytest.raises(ToolError) as exc:
            server.query(question="what is X?", exclude_tags=["a", "b"])

    msg = str(exc.value).lower()
    assert "exclude_tags" in msg or "size" in msg or "at most one" in msg
    # The orchestrator must not have been invoked — validation lives at
    # the boundary, before any retrieval runs.
    orch_cls.return_value.run_query.assert_not_called()


def test_mcp_query_unknown_tag(monkeypatch, fake_wiki_with_collections) -> None:
    """An unknown include atom raises ``ToolError`` with the exact spelling.

    Spec error model: ``TagExprUnknown`` → ``ToolError`` with the
    verbatim tag. No fuzzy match, no did-you-mean — the operator owns
    the spelling.
    """
    from fastmcp.exceptions import ToolError

    from lies.mcp import server

    with mock.patch.object(server, "Orchestrator") as orch_cls:
        with pytest.raises(ToolError, match="unknown tag: nope"):
            server.query(question="what is X?", tag_expr="nope")

    orch_cls.return_value.run_query.assert_not_called()


def test_mcp_query_exclude_tags_solo_passes_through(
    monkeypatch, fake_wiki_with_collections
) -> None:
    """``exclude_tags=['amazon']`` alone (no ``tag_expr``) is a valid filter.

    The size ≤ 1 limit applies; a single-element list resolves to a
    ``ResolvedTagFilter(exclude='amazon')`` with no include chain.
    """
    from lies.mcp import server

    fake = _answer()

    with mock.patch.object(server, "Orchestrator") as orch_cls:
        orch_cls.return_value.run_query.return_value = fake
        server.query(question="what is X?", exclude_tags=["amazon"])

    call = orch_cls.return_value.run_query.call_args
    tag_filter = call.kwargs["tag_filter"]
    assert tag_filter is not None
    assert tag_filter.include is None
    assert tag_filter.exclude == "amazon"


def test_mcp_query_no_tag_kwargs_back_compat(monkeypatch, fake_wiki_with_collections) -> None:
    """No ``tag_expr`` / ``exclude_tags`` → ``tag_filter=None`` to orchestrator.

    Pin the additive contract: existing callers (no tag kwargs) reach
    the orchestrator with ``tag_filter=None`` — the unfiltered path.
    """
    from lies.mcp import server

    fake = _answer()

    with mock.patch.object(server, "Orchestrator") as orch_cls:
        orch_cls.return_value.run_query.return_value = fake
        server.query(question="what is X?")

    call = orch_cls.return_value.run_query.call_args
    assert call.kwargs["tag_filter"] is None


def test_mcp_query_parse_error_raises_tool_error(monkeypatch, fake_wiki_with_collections) -> None:
    """``tag_expr='airflow&'`` (dangling operator) → ``ToolError`` at boundary.

    Spec error model: ``TagExprParseError`` → ``ToolError`` with the
    verbatim grammar error. The parser's role is grammar; the MCP layer
    catches parse errors at the boundary so LLM callers see a useful
    message instead of FastMCP's default ``-32603 internal error``.
    """
    from fastmcp.exceptions import ToolError

    from lies.mcp import server

    with mock.patch.object(server, "Orchestrator") as orch_cls:
        with pytest.raises(ToolError) as exc:
            server.query(question="what is X?", tag_expr="airflow&")

    msg = str(exc.value).lower()
    assert "invalid tag expression" in msg or "tag expression" in msg
    # The orchestrator must not have been invoked — validation lives at
    # the boundary, before any retrieval runs.
    orch_cls.return_value.run_query.assert_not_called()


def test_mcp_query_empty_tag_expr_raises_tool_error(
    monkeypatch, fake_wiki_with_collections
) -> None:
    """``tag_expr=''`` → ``ToolError`` at boundary.

    Spec error model: ``TagExprEmpty`` → ``ToolError``. An empty include
    expression is a user error, not a silent no-op; the MCP layer
    catches it at the boundary so LLM callers see a useful message
    instead of FastMCP's default ``-32603 internal error``.
    """
    from fastmcp.exceptions import ToolError

    from lies.mcp import server

    with mock.patch.object(server, "Orchestrator") as orch_cls:
        with pytest.raises(ToolError) as exc:
            server.query(question="what is X?", tag_expr="")

    msg = str(exc.value).lower()
    assert "empty tag expression" in msg or "tag expression" in msg
    # The orchestrator must not have been invoked — validation lives at
    # the boundary, before any retrieval runs.
    orch_cls.return_value.run_query.assert_not_called()
