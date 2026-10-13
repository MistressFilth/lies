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
    """Reachable daemon + healthy liveness probe -> MCP(local=QmdRecycleToolset)."""
    from pydantic_ai.capabilities import MCP

    from lies.qmd.mcp import QmdRecycleToolset

    layout = WikiLayout(wiki_root)
    monkeypatch.setattr("lies.qmd.capability.qmd_daemon_reachable", lambda url, timeout=0.5: True)
    # Construction-time liveness probe (a fastmcp.Client.list_tools()
    # round-trip) must succeed for the native path. Stub the probe so
    # the test never opens a real socket.

    async def _probe_ok(url: str) -> None:
        return None

    monkeypatch.setattr("lies.qmd.capability._probe_liveness", _probe_ok)
    cap = QmdCapability(transport="http", url="http://127.0.0.1:8181", wiki=layout).as_capability()
    assert isinstance(cap, MCP)
    # Native path now wraps the inner MCPToolset in a QmdRecycleToolset
    # and exposes it through ``MCP(local=...)``.
    assert cap.native is False
    assert cap.local is not None
    toolset = cap.local.function()
    assert isinstance(toolset, QmdRecycleToolset)


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
    """The factory the capability builds for the local branch must, when called, return an MCPToolset wrapping the in-process FastMCP server."""
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.tools import Tool

    from tests.conftest import make_wiki

    wiki = make_wiki(name="capability-fallback", data_root=wiki_root)
    monkeypatch.setattr("lies.qmd.capability.qmd_daemon_reachable", lambda url, timeout=0.5: False)
    cap = QmdCapability(transport="http", url="http://127.0.0.1:8181", wiki=wiki).as_capability()
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
    """If the probe flips from False to True between calls, the capability advertises the native QmdRecycleToolset again on the next call."""
    from lies.qmd.mcp import QmdRecycleToolset

    layout = WikiLayout(wiki_root)
    probes = iter([False, True])
    monkeypatch.setattr(
        "lies.qmd.capability.qmd_daemon_reachable",
        lambda url, timeout=0.5: next(probes),
    )

    async def _probe_ok(url: str) -> None:
        return None

    monkeypatch.setattr("lies.qmd.capability._probe_liveness", _probe_ok)
    cap_ctor = QmdCapability(transport="http", url="http://127.0.0.1:8181", wiki=layout)
    first = cap_ctor.as_capability()
    assert first.native is False
    second = cap_ctor.as_capability()
    assert second.native is False
    assert isinstance(second.local.function(), QmdRecycleToolset)


def test_unknown_transport_raises(wiki_root: Path) -> None:
    layout = WikiLayout(wiki_root)
    with pytest.raises(ValueError, match="Unknown transport"):
        QmdCapability(transport="bogus", wiki=layout)


def test_qmd_capability_falls_back_when_liveness_probe_fails(
    wiki_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construction-time liveness probe failure -> in-process fallback MCP."""
    import httpx

    from tests.conftest import make_wiki

    # Build a real Wiki (``WikiLayout`` lacks ``registry_path`` which
    # the fallback factory needs when it dereferences the registry)
    # using the test conftest's ``make_wiki`` factory.
    wiki = make_wiki(name="probe-fallback", data_root=wiki_root)

    # Reachable at construction (passes the TCP probe).
    monkeypatch.setattr(
        "lies.qmd.capability.qmd_daemon_reachable",
        lambda url, timeout=0.5: True,
    )

    # But the JSON-RPC probe itself fails (e.g. daemon accepted the TCP
    # connection but never finished initializing). httpx.ConnectError
    # is a realistic transport-level signal — it covers refused, reset,
    # and unreachable daemons.
    async def _probe_fails(url: str) -> None:
        raise httpx.ConnectError("probed daemon refused / reset")

    monkeypatch.setattr("lies.qmd.capability._probe_liveness", _probe_fails)

    cap = QmdCapability(transport="http", url="http://127.0.0.1:8181", wiki=wiki).as_capability()
    # Positive assertion: the fallback MCP describes the in-process path,
    # NOT the native qmd daemon. This pins the branch we picked.
    assert cap.id == "lies.qmd"
    assert "Falls back to a degraded in-process index scan" in cap.description
    assert not cap.description.startswith(
        "qmd MCP daemon. Search the wiki, read pages, and check collection status."
    )
    # The factory under ``cap.local`` must resolve to the fallback's
    # MCPToolset, not the native QmdRecycleToolset.
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.tools import Tool

    assert isinstance(cap.local, (Tool, MCPToolset)) or callable(cap.local)
    if isinstance(cap.local, Tool):
        toolset = cap.local.function()
        assert isinstance(toolset, MCPToolset)
    elif isinstance(cap.local, MCPToolset):
        # Already a toolset.
        pass
    # The MCP we built is the fallback, not the native one — confirmed
    # by the description substring above.


def test_as_capability_reaps_when_daemon_is_stale(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale daemon at construction -> reap+respawn before advertising toolset."""
    from pydantic_ai.capabilities import MCP

    from tests.conftest import make_wiki

    wiki = make_wiki(name="capability-stale-reap", data_root=wiki_root)
    monkeypatch.setattr("lies.qmd.capability.qmd_daemon_reachable", lambda url, timeout=0.5: True)

    reap_calls: list[bool] = []
    spawn_calls: list[dict] = []
    sidecar_calls: list[Path] = []

    monkeypatch.setattr(
        "lies.qmd.capability._reap_qmd_daemon",
        lambda *, grace=2.0, poll=0.05: reap_calls.append(True),
    )
    monkeypatch.setattr(
        "lies.qmd.capability._spawn_qmd_daemon",
        lambda **kw: spawn_calls.append(kw),
    )
    monkeypatch.setattr(
        "lies.qmd.capability.write_sidecar_scope",
        lambda scope: sidecar_calls.append(scope),
    )
    monkeypatch.setattr("lies.qmd.capability._is_daemon_stale", lambda: True)

    async def _probe_ok(url: str) -> None:
        return None

    monkeypatch.setattr("lies.qmd.capability._probe_liveness", _probe_ok)

    cap = QmdCapability(transport="http", url="http://127.0.0.1:8181", wiki=wiki).as_capability()
    assert isinstance(cap, MCP)
    assert len(reap_calls) == 1
    assert len(spawn_calls) == 1
    assert len(sidecar_calls) == 1
    # The canonical scope, NOT ``wiki.data_root``. This path used to
    # record the wiki's root, which is a fourth disagreeing value for a
    # parameter that does not select an index; see
    # ``daemon._canonical_scope``.
    from lies.library.registry import library_git_root

    assert sidecar_calls[0] == library_git_root()
    assert sidecar_calls[0] != wiki.data_root
    # And the spawn is told which port LIES is pointed at, so a
    # non-default ``LIES_QMD_URL`` does not get a daemon on 8181.
    assert spawn_calls[0]["port"] == 8181


def test_as_capability_does_not_reap_when_daemon_is_fresh(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh daemon at construction -> no reap envelope, native toolset advertised."""
    from pydantic_ai.capabilities import MCP

    from tests.conftest import make_wiki

    wiki = make_wiki(name="capability-fresh-noop", data_root=wiki_root)
    monkeypatch.setattr("lies.qmd.capability.qmd_daemon_reachable", lambda url, timeout=0.5: True)

    reap_calls: list[bool] = []
    spawn_calls: list[dict] = []
    sidecar_calls: list[Path] = []

    monkeypatch.setattr(
        "lies.qmd.capability._reap_qmd_daemon",
        lambda *, grace=2.0, poll=0.05: reap_calls.append(True),
    )
    monkeypatch.setattr(
        "lies.qmd.capability._spawn_qmd_daemon",
        lambda **kw: spawn_calls.append(kw),
    )
    monkeypatch.setattr(
        "lies.qmd.capability.write_sidecar_scope",
        lambda scope: sidecar_calls.append(scope),
    )
    monkeypatch.setattr("lies.qmd.capability._is_daemon_stale", lambda: False)

    async def _probe_ok(url: str) -> None:
        return None

    monkeypatch.setattr("lies.qmd.capability._probe_liveness", _probe_ok)

    cap = QmdCapability(transport="http", url="http://127.0.0.1:8181", wiki=wiki).as_capability()
    assert isinstance(cap, MCP)
    assert reap_calls == []
    assert spawn_calls == []
    assert sidecar_calls == []


def test_as_capability_falls_back_when_native_probe_fails_after_reap(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale + reap succeeds + JSON-RPC probe fails -> in-process fallback."""
    import httpx

    from tests.conftest import make_wiki

    wiki = make_wiki(name="capability-reap-then-fallback", data_root=wiki_root)
    monkeypatch.setattr("lies.qmd.capability.qmd_daemon_reachable", lambda url, timeout=0.5: True)
    monkeypatch.setattr("lies.qmd.capability._is_daemon_stale", lambda: True)
    monkeypatch.setattr(
        "lies.qmd.capability._reap_qmd_daemon", lambda *, grace=2.0, poll=0.05: None
    )
    monkeypatch.setattr("lies.qmd.capability._spawn_qmd_daemon", lambda **kw: None)
    monkeypatch.setattr("lies.qmd.capability.write_sidecar_scope", lambda scope: None)

    async def _probe_fails(url: str) -> None:
        raise httpx.ConnectError("freshly-spawned daemon refused connection")

    monkeypatch.setattr("lies.qmd.capability._probe_liveness", _probe_fails)

    cap = QmdCapability(transport="http", url="http://127.0.0.1:8181", wiki=wiki).as_capability()
    assert cap.id == "lies.qmd"
    assert "Falls back to a degraded in-process index scan" in cap.description
    assert not cap.description.startswith(
        "qmd MCP daemon. Search the wiki, read pages, and check collection status."
    )
