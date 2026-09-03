"""MCP `query` tool kwargs (F3 file-back) — collection / file / force_file.

The `query` MCP tool mirrors the `lies query` CLI flags of the same
name. The MCP layer itself must stay a thin shim: ``Orchestrator.run_query``
already accepts those kwargs, so the tool forwards them and only adds a
typed ``ToolError`` envelope around ``WikiPlanInvalid`` raised by the
orchestrator when ``file=True`` and the caller didn't supply
``collection``.

Tests patch ``lies.mcp.server.resolve_wiki`` and
``lies.mcp.server.Orchestrator`` so they don't depend on a real wiki,
real LLM, or real qmd.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from lies.memory.models import (
    MemoryReceipt,
    PageReference,
    WikiPlanInvalid,
)
from lies.query.models import SynthesizedAnswer


def _wiki(tmp_path: Path):
    """Build a 6-field Wiki rooted at ``tmp_path``; no I/O required."""
    from lies.wiki.wiki import Wiki

    return Wiki(
        name="t",
        data_root=tmp_path,
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )


def _answer(**kwargs: Any) -> SynthesizedAnswer:
    """Default-constructed SynthesizedAnswer with optional overrides."""
    return SynthesizedAnswer(
        answer="x",
        fallback_used=False,
        fallback_reason="",
        **kwargs,
    )


def test_mcp_query_forwards_collection_to_orchestrator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`collection="c"` on the MCP tool reaches ``Orchestrator.run_query``."""
    from lies.mcp import server
    from lies.mcp.server import query

    wiki = _wiki(tmp_path)
    monkeypatch.setattr(server, "resolve_wiki", lambda _name=None: wiki)

    fake = _answer()
    with mock.patch.object(server, "Orchestrator") as orch_cls:
        orch_cls.return_value.run_query.return_value = fake
        query("what?", name="t", collection="c", file=True, force_file=False)

    orch_cls.return_value.run_query.assert_called_once_with(
        "what?",
        collection="c",
        file=True,
        force_file=False,
    )
    # And the instance was built with the wiki the resolver returned.
    orch_cls.assert_called_once_with(wiki=wiki)


def test_mcp_query_missing_collection_raises_tool_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing collection with ``should_file=True`` raises a typed ``ToolError``.

    ``Orchestrator.run_query`` raises :class:`WikiPlanInvalid` when the
    caller asks for a filing (``file=True`` + ``should_file=True`` /
    ``force_file=True``) but does not supply ``collection``. The MCP
    layer catches the typed error and re-raises it as ``ToolError`` so
    LLM callers can react to the failure rather than silently losing
    the filing intent.
    """
    from fastmcp.exceptions import ToolError

    from lies.mcp import server
    from lies.mcp.server import query

    monkeypatch.setattr(server, "resolve_wiki", lambda _name=None: _wiki(tmp_path))

    with mock.patch.object(server, "Orchestrator") as orch_cls:
        orch_cls.return_value.run_query.side_effect = WikiPlanInvalid(
            "collection required to file synthesis"
        )
        with pytest.raises(ToolError, match="collection required"):
            query("what?", name="t", file=True, force_file=True)


def test_mcp_query_no_file_skips_file_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`file=False` reaches ``run_query``; the tool does not call ``file_back_synthesis``."""
    from lies.mcp import server
    from lies.mcp.server import query

    monkeypatch.setattr(server, "resolve_wiki", lambda _name=None: _wiki(tmp_path))

    fake = _answer(should_file=True)
    with mock.patch.object(server, "Orchestrator") as orch_cls:
        orch_cls.return_value.run_query.return_value = fake
        result = query("what?", name="t", file=False)

    orch_cls.return_value.run_query.assert_called_once_with(
        "what?",
        collection=None,
        file=False,
        force_file=False,
    )
    # file=False means no filing; the answer's should_file is still
    # carried but file_receipt stays None.
    assert result.should_file is True
    assert result.file_receipt is None


def test_mcp_synthesized_answer_carries_file_receipt_dict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`file_receipt` on the MCP slice serializes as ``dict | None``."""
    from lies.mcp import server
    from lies.mcp.server import SynthesizedMcpAnswer, query

    monkeypatch.setattr(server, "resolve_wiki", lambda _name=None: _wiki(tmp_path))

    receipt = MemoryReceipt(
        changed_pages=[PageReference(path="wiki/c/synthesis/x.md", collection_id="c", op="create")],
        deferred=[],
        fallback_used=False,
        fallback_reason="",
        errors=[],
    )
    fake = _answer(file_receipt=receipt)
    with mock.patch.object(server, "Orchestrator") as orch_cls:
        orch_cls.return_value.run_query.return_value = fake
        result = query("what?", name="t")

    assert isinstance(result, SynthesizedMcpAnswer)
    assert isinstance(result.file_receipt, dict)
    assert result.file_receipt["changed_pages"][0]["path"] == "wiki/c/synthesis/x.md"
    # Non-filed path keeps the field at None.
    with mock.patch.object(server, "Orchestrator") as orch_cls:
        orch_cls.return_value.run_query.return_value = _answer()
        result = query("what?", name="t")

    assert result.file_receipt is None


def test_mcp_query_synthesized_answer_carries_should_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The MCP slice surfaces ``should_file`` so callers can drive F3."""
    from lies.mcp import server
    from lies.mcp.server import query

    monkeypatch.setattr(server, "resolve_wiki", lambda _name=None: _wiki(tmp_path))

    with mock.patch.object(server, "Orchestrator") as orch_cls:
        orch_cls.return_value.run_query.return_value = _answer(should_file=True)
        result = query("what?", name="t", file=False)

    assert result.should_file is True
