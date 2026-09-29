"""collections_read MCP tool — live registry reader for the librarian LLM.

Exposes ``name``, ``tags``, ``scope_keywords`` per row in a flat,
three-subcommand shape so the librarian LLM can build ``tag_expr``
from the live registry in Step 1 of the 4-step pipeline.

Subcommands:

- ``list``     → one row per registered collection
- ``tag_list`` → tag → collections map (qualifier prefix stripped)
- ``info`` + ``name=…`` → single collection's full metadata

Specs: docs/superpowers/specs/2026-09-26-librarian-v040-port-design.md.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp.exceptions import ToolError


def collections_read(
    subcommand: Literal["list", "tag_list", "info"],
    name: str | None = None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Read the live library registry.

    Args:
        subcommand: ``list`` returns row-per-collection; ``tag_list`` returns
            tag → collections map (qualifier-prefixed atoms surfaced as
            bare tag); ``info`` returns a single row's full metadata and
            requires ``name``.
        name: Required for ``subcommand="info"``. The collection to inspect.

    Returns:
        ``list[dict]`` for ``list`` and ``tag_list`` returns ``dict``;
        ``dict`` for ``info``. Empty registry returns empty list / empty dict.

    Raises:
        ToolError: ``info`` called without ``name``, or with an unknown name.
    """
    # Lazy imports — keep this module off the library-registry import chain
    # at module load time (the FastMCP mount calls this lazily via tool
    # dispatch, not at registration).
    from lies.library.registry import library_collection_metas

    metas = list(library_collection_metas())

    if subcommand == "list":
        return [
            {
                "name": m.name,
                "source_url": m.source_url,
                "tags": sorted(m.tags),
                "scope_keywords": sorted(m.scope_keywords),
            }
            for m in metas
        ]

    if subcommand == "tag_list":
        out: dict[str, list[str]] = {}
        for m in metas:
            for tag in m.tags:
                # Strip the c:/t: qualifier prefix; callers infer
                # the qualifier from the collection row when needed.
                bare = tag.split(":", 1)[-1] if ":" in tag else tag
                out.setdefault(bare, []).append(m.name)
        # Sort values for determinism.
        return {k: sorted(v) for k, v in sorted(out.items())}

    if subcommand == "info":
        if not name:
            raise ToolError("collections_read: subcommand='info' requires name")
        for m in metas:
            if m.name == name:
                return {
                    "name": m.name,
                    "source_url": m.source_url,
                    "tags": sorted(m.tags),
                    "scope_keywords": sorted(m.scope_keywords),
                }
        raise ToolError(f"collection not registered: {name!r}")

    raise ToolError(f"unknown subcommand: {subcommand!r}")
