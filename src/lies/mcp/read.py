"""read MCP tool — verbatim page bodies via source-aware dispatch.

Dispatches wiki page IDs (``page-…``) to ``memory_service.read()``
and library paths (``<collection>/<page>``) to the qmd daemon's
``get`` with ``lineNumbers: false`` (the CLI line-numbers by
default; ``--no-line-numbers`` still leaves a ``qmd://path``
header).

The body is the document and nothing else — the citation
contract is ``[[slug]]: "verbatim quote"``. ``daemon_tool``
returns a raw ``CallToolResult``; ``.data`` is ``None`` on
both tools; the text lives at
``.content[].resource.text``. ``_resource_texts`` reads
bodies; ``_notices`` reads TextContent (``[SKIPPED: …]``,
``Errors: …``).

One ``get`` per path, never ``multi_get``: 1854 of this
corpus's 5987 documents are over ``multi_get``'s 10KB default
and arrive *skipped*, so batching silently drops nearly a
third of what a reader can ask for. ``get`` has no size cap.

A down or wedged daemon re-raises; both spellings of "no body
for this path" are treated identically (qmd's implementation
detail); a batch that yields nothing raises
``ToolError("all reads failed")`` once."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool

from lies.qmd import access
from lies.qmd.access import QmdDaemonUnavailable, QmdDaemonWedged

log = logging.getLogger(__name__)

_DAEMON_FAILURES = (QmdDaemonUnavailable, QmdDaemonWedged)


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


def _daemon_body(path: str) -> str | None:
    """One body for ``path``; ``None`` if qmd produced no body (raised or
    only-notices). A down or wedged daemon raises before reaching here.
    """
    try:
        result = _run_blocking(access.daemon_tool("get", {"file": path, "lineNumbers": False}))
    except _DAEMON_FAILURES:
        raise
    except Exception as exc:
        log.warning("read: qmd get(%s) failed: %s", path, exc)
        return None

    bodies = _resource_texts(result)
    if not bodies:
        notices = _notices(result)
        detail = f"; qmd said: {' | '.join(notices)}" if notices else ""
        log.warning("read: qmd get(%s) returned no document body%s", path, detail)
        return None
    return "\n".join(bodies)


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

    for p in library_paths:
        body = _daemon_body(p)
        if body is not None:
            out[p] = body

    if paths and not out:
        raise ToolError("all reads failed")

    return out


read = Tool.from_function(_read_impl, name="read")
