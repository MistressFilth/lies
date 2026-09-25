"""Server-shape tests for the library-mode MCP resource surface.

Asserts the wire-shape contract after the read-side rewrite:
the retired ``wiki://*`` resources (wiki://page/{path},
wiki://memory-changes, wiki://catalog{,/{slug}}) are gone, replaced
by ``library://catalog{,/{slug}}``. The static ``wiki://status``,
``wiki://index``, ``wiki://log``, and ``wiki://lint-report``
resources are kept (the brief lists only the four
data-bearing resources for replacement).

Uses FastMCP's internal ``_list_resources`` and
``_list_resource_templates`` rather than the wire ``Client`` path
— the Client path costs ~0.19s on this machine (well over the
0.15s pre-commit hard limit), and the wire shape returns the
same records ``_list_*`` exposes.
"""

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
