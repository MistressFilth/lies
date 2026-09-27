"""read MCP tool — verbatim page bodies via source-aware dispatch.

Replaces the old ``wiki_read`` tool. Dispatches:

- wiki page IDs (``page-…``) → ``memory_service.read()``
- library paths (``<collection>/<page>``) → ``qmd_get()``

Failures per path: log + skip. All-path failure: raise ToolError.
Spec: docs/superpowers/specs/2026-09-26-librarian-v040-port-design.md.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

log = logging.getLogger(__name__)


def _qmd_get(cwd: Path, qmd_path: str, timeout: int = 60) -> str:
    """Wrapper around lies.qmd.cli.qmd_get. Patched in tests."""
    from lies.qmd.cli import qmd_get as _impl

    return _impl(cwd, qmd_path, timeout=timeout)


def _memory_service() -> Any:
    """Lazy accessor for the wiki memory service.

    Returns a stub that raises if anyone actually tries to call it
    before the orchestrator wires a real service in. Production callers
    re-bind this attribute at module import time in Task 9.
    """
    raise RuntimeError("read._memory_service not wired")


def _read_impl(paths: list[str]) -> dict[str, str]:
    """Read verbatim bodies for a mix of wiki page IDs and library paths."""
    if not paths:
        return {}

    wiki_ids: list[str] = []
    library_paths: list[str] = []
    for p in paths:
        if p.startswith("page-"):
            wiki_ids.append(p)
        elif "/" in p:
            library_paths.append(p)
        else:
            log.warning("read: unrecognized path format: %r (skipped)", p)

    out: dict[str, str] = {}

    if wiki_ids:
        try:
            wiki_out = _memory_service().read(wiki_ids)
        except Exception as exc:
            log.warning("read: memory_service.read failed: %s", exc)
            wiki_out = {}
        out.update(wiki_out)

    if library_paths:
        from lies.library.registry import library_git_root

        lib_root = library_git_root()
        for p in library_paths:
            try:
                body = _qmd_get(lib_root, f"qmd://{p}")
                out[p] = body
            except Exception as exc:
                log.warning("read: qmd_get(%s) failed: %s", p, exc)

    if paths and not out:
        raise ToolError("all reads failed")

    return out


# Wrap as a FastMCP ``Tool`` so the MCP wire can serialize the dispatch
# surface and tests can reach the underlying function via ``read.fn(...)``.
# Mirrors the pattern in ``search.py`` (Task 4).
read = Tool.from_function(_read_impl, name="read")
