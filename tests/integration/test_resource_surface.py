"""End-to-end test that the MCP server exposes the expected tool/resource surface.

Task 4 of the library-mode read-side rewrite: after Task 3 retired
the wiki-shaped read tools (``query`` / ``answer`` / ``wiki_search``
/ ``wiki_read`` / ``wiki_changes`` / ``file_knowledge``) and the
wiki-shaped data resources (``wiki://page/{path}`` /
``wiki://memory-changes`` / ``wiki://catalog`` /
``wiki://catalog/{slug}``) in favor of ``synthesize`` /
``library://catalog``, this integration test pins the post-Task-3
surface end-to-end via a real ``fastmcp.Client``.

The brief's `test_list_resources_excludes_wiki_and_qmd` shape is
relaxed here: the operational diagnostics (``wiki://status`` /
``wiki://index`` / ``wiki://log`` / ``wiki://lint-report``) stay on
the surface as Task 3's report noted — they have no library-side
equivalent in this rewrite. The test pins the *retired* wiki:// URIs
specifically rather than every ``wiki://`` URI.

Gated on ``INTEGRATION=1`` via the integration conftest's
``pytest_collection_modifyitems`` hook. Default ``make test`` and
``make check`` skip these tests; CI runs them with ``INTEGRATION=1``.
"""

from __future__ import annotations

import asyncio


def test_list_tools_returns_expected_surface():
    """The MCP tool surface is the documented post-Task-3 set.

    Pins every registered tool. The set must match exactly — drift
    on this list (either new tool leaking in or a documented tool
    going missing) surfaces as an immediate integration failure, not
    a silent regression caught later by ad-hoc callers.
    """
    from fastmcp import Client

    from lies.mcp.server import mcp

    async def main():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            expected = {
                "ground",
                "synthesize",
                "init_wiki",
                "lint",
                "reindex",
                "ask_ground_question",
                "ask_question",
            }
            assert names == expected, f"got {names!r}, expected {expected!r}"

    asyncio.run(main())


def test_list_resources_excludes_retired_wiki_data_and_qmd():
    """The MCP resource surface drops retired wiki:// data + qmd://, adds library://catalog.

    Task 3 retired these URIs:
      - ``wiki://page/{path}`` (wiki-shaped data resource)
      - ``wiki://memory-changes`` (memory sidecar resource)
      - ``wiki://catalog`` / ``wiki://catalog/{slug}`` (catalog resource)

    Task 3 added:
      - ``library://catalog`` (static)
      - ``library://catalog/{slug}`` (template)

    The operational diagnostics — ``wiki://status`` / ``wiki://index``
    / ``wiki://log`` / ``wiki://lint-report`` — stay on the surface
    because they have no library-side equivalent in this rewrite;
    this test does not assert against them.

    ``qmd://*`` is never registered on the LIES MCP surface — that's
    the spec preamble's visibility rule, pinned here so a future
    refactor doesn't accidentally expose qmd's internal schema.
    """
    from fastmcp import Client

    from lies.mcp.server import mcp

    async def main():
        async with Client(mcp) as client:
            # Static resources
            static = await client.list_resources()
            static_uris = {str(r.uri) for r in static}
            retired_static = {"wiki://page", "wiki://memory-changes", "wiki://catalog"}
            overlap = static_uris & retired_static
            assert not overlap, f"retired wiki:// static resources still registered: {overlap!r}"
            assert "library://catalog" in static_uris, (
                f"library://catalog missing from static resources: {static_uris!r}"
            )

            # Resource templates (the {slug}-shaped ones)
            templates = await client.list_resource_templates()
            template_uris = {t.uriTemplate for t in templates}
            retired_templates = {
                "wiki://page/{path}",
                "wiki://catalog/{slug}",
            }
            template_overlap = template_uris & retired_templates
            assert not template_overlap, (
                f"retired wiki:// templates still registered: {template_overlap!r}"
            )
            assert "library://catalog/{slug}" in template_uris, (
                f"library://catalog/{{slug}} missing from templates: {template_uris!r}"
            )

            # qmd:// must never surface on the LIES MCP surface
            assert not any(u.startswith("qmd://") for u in static_uris), (
                f"qmd:// URIs leaked into the LIES surface: "
                f"{[u for u in static_uris if u.startswith('qmd://')]!r}"
            )
            assert not any(t.startswith("qmd://") for t in template_uris), (
                f"qmd:// templates leaked into the LIES surface: "
                f"{[t for t in template_uris if t.startswith('qmd://')]!r}"
            )

    asyncio.run(main())
