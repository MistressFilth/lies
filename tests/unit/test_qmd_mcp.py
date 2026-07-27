from __future__ import annotations

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
