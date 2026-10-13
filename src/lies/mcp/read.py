"""read MCP tool — verbatim page bodies via source-aware dispatch."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

from lies.qmd import access

log = logging.getLogger(__name__)


def _memory_service() -> Any:
    """Stub; raises until the orchestrator wires a real service in."""
    raise RuntimeError("read._memory_service not wired")


def _resource_texts(result: Any) -> list[str]:
    """Bodies: EmbeddedResource ``text`` fields. ``.data`` is always None."""
    blocks = getattr(result, "content", None) or []
    return [
        t
        for t in (getattr(getattr(block, "resource", None), "text", None) for block in blocks)
        if isinstance(t, str)
    ]


def _notices(result: Any) -> list[str]:
    """TextContent blocks (``[SKIPPED: …]``, ``Errors: …``) — never a body."""
    blocks = getattr(result, "content", None) or []
    return [t for t in (getattr(block, "text", None) for block in blocks) if isinstance(t, str)]


def _run_blocking(coro: Any) -> Any:
    """Drain a coroutine from sync code, with or without a running loop.

    FastMCP runs sync handlers in an executor with no loop, so
    ``asyncio.run`` is the whole story there. A genuinely running
    loop gets its own thread and its own loop — ``asyncio.run``
    from inside a running loop raises ``RuntimeError``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="lies-read") as pool:
        return pool.submit(asyncio.run, coro).result()


def _daemon_bodies_batched(paths: list[str]) -> list[str | None]:
    """One body per path over a single MCP session."""
    if not paths:
        return []
    results = _run_blocking(access.read_library_bodies(paths))

    bodies: list[str | None] = []
    for path, result in zip(paths, results, strict=True):
        if result is None or getattr(result, "is_error", False):
            log.warning("read: qmd get(%s) returned no document body", path)
            bodies.append(None)
            continue
        text_blocks = _resource_texts(result)
        if not text_blocks:
            notices = _notices(result)
            detail = f"; qmd said: {' | '.join(notices)}" if notices else ""
            log.warning("read: qmd get(%s) returned no document body%s", path, detail)
            bodies.append(None)
            continue
        bodies.append("\n".join(text_blocks))
    return bodies


_MISSING_KEY = "_missing"


def _read_impl(paths: list[str]) -> dict[str, str | list[str]]:
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

    out: dict[str, str | list[str]] = {}

    if wiki_ids:
        try:
            wiki_out = _memory_service().read(wiki_ids)
        except Exception as exc:
            log.warning("read: memory_service.read failed: %s", exc)
            wiki_out = {}
        out.update(wiki_out)

    missing: list[str] = []
    if library_paths:
        # One MCP session for the whole batch. Measured against the live daemon: one-shot session
        # p50 75.4 ms versus a persistent session p50 41.9 ms per call, so a 20-path read drops
        # from ~1.5 s to ~900 ms.
        for p, body in zip(library_paths, _daemon_bodies_batched(library_paths), strict=True):
            if body is None:
                missing.append(p)
            else:
                out[p] = body

    # Paths the wiki service omitted from its result (asked for
    # 5 IDs, received 4) are also a soft missing.
    for p in wiki_ids:
        if p not in out:
            missing.append(p)

    if paths and not out:
        raise ToolError("all reads failed")

    if missing:
        out[_MISSING_KEY] = missing
    return out


read = Tool.from_function(_read_impl, name="read")
