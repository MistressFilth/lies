"""Server-shape tests for the library-mode MCP resource surface."""

from __future__ import annotations

from lies.mcp.server import mcp


async def test_server_registers_library_catalog_resources() -> None:
    """After the rewrite, library://catalog{,/{slug}} replace wiki://*."""
    resources = await mcp._list_resources()
    templates = await mcp._list_resource_templates()
    resource_uris = {str(r.uri) for r in resources}
    template_patterns = {t.uri_template for t in templates}
    all_patterns = resource_uris | template_patterns

    # New library resources must be present.
    assert "library://catalog" in resource_uris
    assert "library://catalog/{slug}" in template_patterns

    # Retired wiki resources must be gone.
    assert not any(p.startswith("wiki://page/") for p in all_patterns)
    assert not any(p.startswith("wiki://memory-changes") for p in all_patterns)
    assert not any(p.startswith("wiki://catalog") for p in all_patterns)
