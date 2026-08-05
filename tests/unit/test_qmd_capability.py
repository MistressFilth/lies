from __future__ import annotations

from pathlib import Path

import pytest

from lies.qmd.capability import QmdCapability
from lies.wiki.layout import WikiLayout


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    (tmp_path / "raw").mkdir()
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".lies").mkdir()
    return tmp_path


def test_stdio_transport_uses_local_toolset(wiki_root: Path) -> None:
    """LIES_QMD_TRANSPORT=stdio must keep building a local toolset."""
    from pydantic_ai.capabilities import MCP
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.tools import Tool

    layout = WikiLayout(wiki_root)
    cap = QmdCapability(transport="stdio", wiki=layout).as_capability()
    assert isinstance(cap, MCP)
    # ``native`` is left at its default (``False``) for the stdio branch.
    assert cap.native is False
    # pydantic-ai wraps the factory callable in a ``Tool``. Accept any of
    # the shapes ``MCP`` may legitimately produce — ``Tool`` (the wrapped
    # factory), a raw ``MCPToolset``, or the bare callable.
    assert cap.local is not None
    assert isinstance(cap.local, (Tool, MCPToolset)) or callable(cap.local)


def test_http_capability_advertises_native_when_daemon_reachable(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reachable daemon -> MCP(url=..., native=MCPServerTool, local=False)."""
    from pydantic_ai.capabilities import MCP
    from pydantic_ai.native_tools import MCPServerTool

    layout = WikiLayout(wiki_root)
    monkeypatch.setattr("lies.qmd.capability.qmd_daemon_reachable", lambda url, timeout=0.5: True)
    cap = QmdCapability(transport="http", url="http://127.0.0.1:8181", wiki=layout).as_capability()
    assert isinstance(cap, MCP)
    assert cap.url == "http://127.0.0.1:8181"
    # pydantic-ai turns ``native=True`` into an ``MCPServerTool`` instance;
    # the boolean ``True`` never appears as the attribute value.
    assert isinstance(cap.native, MCPServerTool)
    assert cap.local is False


def test_http_capability_uses_local_when_daemon_unreachable(
    wiki_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unreachable daemon -> MCP(local=factory), native off, one warning."""
    from pydantic_ai.capabilities import MCP
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.tools import Tool

    layout = WikiLayout(wiki_root)
    monkeypatch.setattr("lies.qmd.capability.qmd_daemon_reachable", lambda url, timeout=0.5: False)
    cap = QmdCapability(transport="http", url="http://127.0.0.1:8181", wiki=layout).as_capability()
    captured = capsys.readouterr()
    assert "qmd daemon unreachable" in captured.err
    assert "127.0.0.1:8181" in captured.err
    assert "degraded" in captured.err
    assert isinstance(cap, MCP)
    assert cap.native is False
    # pydantic-ai wraps the factory callable in a ``Tool``. The raw
    # factory is exposed via ``cap.local.function`` (see the next test).
    assert isinstance(cap.local, (Tool, MCPToolset)) or callable(cap.local)


def test_local_factory_yields_an_mcptoolset_over_fallback(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory the capability builds for the local branch must, when
    called, return an MCPToolset wrapping the in-process FastMCP server."""
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.tools import Tool

    layout = WikiLayout(wiki_root)
    monkeypatch.setattr("lies.qmd.capability.qmd_daemon_reachable", lambda url, timeout=0.5: False)
    cap = QmdCapability(transport="http", url="http://127.0.0.1:8181", wiki=layout).as_capability()
    # ``cap.local`` is a ``Tool`` that wraps the raw factory. Reach the
    # factory via ``cap.local.function`` (no-arg invocation — the factory
    # does not take a ``RunContext``) and assert it returns an
    # ``MCPToolset``.
    assert isinstance(cap.local, Tool), (
        "expected pydantic-ai to wrap the factory in a Tool instance"
    )
    toolset = cap.local.function()
    assert isinstance(toolset, MCPToolset)


def test_per_call_recovery_after_daemon_returns(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the probe flips from False to True between calls, the capability
    advertises native again on the next call."""
    from pydantic_ai.native_tools import MCPServerTool

    layout = WikiLayout(wiki_root)
    probes = iter([False, True])
    monkeypatch.setattr(
        "lies.qmd.capability.qmd_daemon_reachable",
        lambda url, timeout=0.5: next(probes),
    )
    cap_ctor = QmdCapability(transport="http", url="http://127.0.0.1:8181", wiki=layout)
    first = cap_ctor.as_capability()
    assert first.native is False
    second = cap_ctor.as_capability()
    assert isinstance(second.native, MCPServerTool)


def test_unknown_transport_raises(wiki_root: Path) -> None:
    layout = WikiLayout(wiki_root)
    with pytest.raises(ValueError, match="Unknown transport"):
        QmdCapability(transport="bogus", wiki=layout)
