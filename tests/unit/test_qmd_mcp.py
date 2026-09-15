from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from lies.qmd.mcp import QmdMcpClient


@pytest.fixture
def client() -> QmdMcpClient:
    return QmdMcpClient(transport="stdio")


def test_client_constructs_with_stdio() -> None:
    c = QmdMcpClient(transport="stdio")
    assert c.transport == "stdio"


def test_client_constructs_with_http() -> None:
    c = QmdMcpClient(transport="http", url="http://localhost:8181")
    assert c.url == "http://localhost:8181"


def test_pydantic_ai_capability() -> None:
    """The MCP client should return a pydantic-ai MCP capability."""
    from pydantic_ai.capabilities import MCP

    cap = QmdMcpClient(transport="stdio").as_capability()

    assert isinstance(cap, MCP)


def test_unknown_transport_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown transport: bogus"):
        QmdMcpClient(transport="bogus").as_capability()


def test_http_capability_uses_configured_url() -> None:
    url = "http://qmd.example.test:8181"

    cap = QmdMcpClient(transport="http", url=url).as_capability()

    assert cap.url == url


def _read_pyproject() -> dict:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as fh:
        return tomllib.load(fh)


def test_pyproject_declares_qmd_extra() -> None:
    """MCP runtime deps are defaults; the empty qmd extra remains for back-compat."""
    project = _read_pyproject()
    extras = project["project"]["optional-dependencies"]
    assert "qmd" in extras, "missing `[qmd]` extra in pyproject.toml"
    default_deps = project["project"]["dependencies"]
    assert any(re.match(r"^fastmcp", d) for d in default_deps), (
        "fastmcp must be a default dependency"
    )
    assert any(re.match(r"^mcp", d) for d in default_deps), "mcp must be a default dependency"
    qmd_extra = project["project"]["optional-dependencies"]["qmd"]
    assert qmd_extra == [], f"qmd extra should be empty, got {qmd_extra!r}"


def test_qmd_extra_locked_in_uv_lock() -> None:
    """The lock resolves MCP deps as defaults without qmd extra markers."""
    lock = Path(__file__).resolve().parents[2] / "uv.lock"
    with lock.open("rb") as fh:
        data = tomllib.load(fh)

    package_names = {p["name"] for p in data["package"]}
    assert "fastmcp" in package_names, "uv.lock is missing fastmcp"
    assert "mcp" in package_names, "uv.lock is missing mcp"

    lies_pkg = next(p for p in data["package"] if p["name"] == "lies")
    default_pkg_names = {r["name"] for r in lies_pkg.get("dependencies", [])}
    assert "fastmcp" in default_pkg_names, "default dependencies must resolve fastmcp in lock"
    assert "mcp" in default_pkg_names, "default dependencies must resolve mcp in lock"
    metadata = lies_pkg.get("metadata", {})
    all_requires_dist = list(metadata.get("requires-dist", []))
    all_req_names = {package["name"] for package in all_requires_dist}
    assert "fastmcp" in all_req_names, "lies requires-dist missing default fastmcp"
    assert "mcp" in all_req_names, "lies requires-dist missing default mcp"
    # No ``requires-dist`` entry should be tagged with the qmd extra marker;
    # the qmd extra is empty, so MCP deps live under the default extras.
    qmd_marked = [
        package["name"]
        for package in all_requires_dist
        if package.get("marker") == "extra == 'qmd'"
    ]
    assert qmd_marked == [], f"qmd extra should be empty, got {qmd_marked!r}"


def test_build_qmd_http_toolset_uses_custom_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_qmd_http_toolset must pass httpx_client_factory to the transport."""
    from lies.qmd import mcp as qmd_mcp

    captured: dict[str, object] = {}

    class _FakeStreamableHttpTransport:
        def __init__(self, url: str, httpx_client_factory=None) -> None:
            captured["url"] = url
            captured["httpx_client_factory"] = httpx_client_factory

    class _FakeClient:
        def __init__(self, transport: object) -> None:
            captured["client_transport"] = transport

    class _FakeToolset:
        def __init__(self, client: object) -> None:
            captured["toolset_client"] = client

    monkeypatch.setattr(qmd_mcp, "StreamableHttpTransport", _FakeStreamableHttpTransport)
    monkeypatch.setattr(qmd_mcp, "fastmcp", type("M", (), {"Client": _FakeClient}))
    monkeypatch.setattr(qmd_mcp, "MCPToolset", _FakeToolset)

    qmd_mcp._build_qmd_http_toolset("http://127.0.0.1:8181")
    assert captured["url"] == "http://127.0.0.1:8181"
    assert captured["httpx_client_factory"] is qmd_mcp._build_qmd_httpx_client


def test_build_qmd_httpx_client_uses_default_timeouts() -> None:
    """_build_qmd_httpx_client must use connect=2.0, read=60.0, write=10.0."""
    from lies.qmd import mcp as qmd_mcp

    client = qmd_mcp._build_qmd_httpx_client()
    assert client.timeout.connect == 2.0
    assert client.timeout.read == 60.0
    assert client.timeout.write == 10.0
    assert client.timeout.pool is not None  # httpx default for pool
