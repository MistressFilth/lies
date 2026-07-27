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
    """The qmd extra is the supported install path for qmd MCP runtime deps.

    Without it, the stdio MCP capability constructs but tool calls fail with
    an ImportError for `fastmcp` / `mcp`. The extra makes the install path
    discoverable and reproducible (via uv.lock).
    """
    data = _read_pyproject()
    extras = data["project"]["optional-dependencies"]
    assert "qmd" in extras, "missing `[qmd]` extra in pyproject.toml"
    qmd_deps = extras["qmd"]
    # Both fastmcp and mcp must be declared in the qmd extra.
    assert any(re.match(r"^fastmcp", d) for d in qmd_deps), (
        "qmd extra must include fastmcp"
    )
    assert any(re.match(r"^mcp", d) for d in qmd_deps), (
        "qmd extra must include mcp"
    )


def test_qmd_extra_locked_in_uv_lock() -> None:
    """The uv.lock file must resolve the qmd extra so installs are reproducible.

    The lock must include both `fastmcp` and `mcp` packages AND the
    `[[package]]` entry for the project must reference them under the qmd
    extra (via `optional-dependencies` and `metadata.requires-dist` with
    `extra == 'qmd'`).
    """
    lock = Path(__file__).resolve().parents[2] / "uv.lock"
    with lock.open("rb") as fh:
        data = tomllib.load(fh)

    package_names = {p["name"] for p in data["package"]}
    assert "fastmcp" in package_names, "uv.lock is missing fastmcp"
    assert "mcp" in package_names, "uv.lock is missing mcp"

    lies_pkg = next(p for p in data["package"] if p["name"] == "lies")
    # `optional-dependencies` is the resolved form in uv.lock
    optional_deps = lies_pkg.get("optional-dependencies", {})
    assert "qmd" in optional_deps, "lies package missing qmd extra in lock"
    qmd_pkg_names = {r["name"] for r in optional_deps["qmd"]}
    assert "fastmcp" in qmd_pkg_names, "qmd extra must resolve fastmcp in lock"
    assert "mcp" in qmd_pkg_names, "qmd extra must resolve mcp in lock"
    # `metadata.requires-dist` carries the original specifier with the marker
    qmd_requires = [
        r
        for r in lies_pkg.get("metadata", {}).get("requires-dist", [])
        if r.get("marker") == "extra == 'qmd'"
    ]
    qmd_req_names = {r["name"] for r in qmd_requires}
    assert "fastmcp" in qmd_req_names, "lies requires-dist missing fastmcp[qmd]"
    assert "mcp" in qmd_req_names, "lies requires-dist missing mcp[qmd]"
