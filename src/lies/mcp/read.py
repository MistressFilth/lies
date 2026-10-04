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
``ToolError("all reads failed")`` once.

Partial batches: paths that ``qmd`` could not resolve are
omitted from the body mapping and surfaced under the
synthetic key ``"_missing"`` as a list of paths. The
``"_"`` prefix keeps the signal out of the path space
(library paths are ``<collection>/<page>``; wiki IDs are
``page-...``; neither begins with an underscore), and the
field's type is a list so a caller iterating ``out.items()``
can filter it with ``key.startswith("_")`` if it is strict
about the body shape. The two-channel
shape — bodies by path, a sibling list of unresolved paths —
is the wire-shape change that closes the silent-drop class
the partial-batch read had: a 20-path read where 3 were
unresolvable previously returned a 17-key dict with
``log.warning`` lines that did not reach the agent."""

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
    """One body per path over a single MCP session.

    Hoists the per-path handshake out of the loop. A down or
    wedged daemon raises out of here — ``read_library_bodies``
    classifies every transport failure, so a slow daemon is a typed
    raise and never a ``None`` entry. Per-path misses reach the
    result loop below as ``None`` and are logged so a partial batch
    still surfaces the missing paths via ``_missing``.

    There is no blanket catch around the call. A previous version
    mapped any exception to all-``None``, which converted a bug in
    the call itself — an ``AttributeError`` from a stale test stub,
    observed in this branch's own integration run — into
    ``ToolError("all reads failed")``. That is a claim about the
    corpus built from a defect in the run, which is the same
    process-versus-corpus confusion this module exists to remove.
    """
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
    """Read verbatim bodies for a mix of wiki page IDs and library paths.

    Returns a dict mapping each successfully read path to its
    body. Paths the daemon (or wiki service) could not resolve
    are *omitted* from the body mapping and surfaced under the
    synthetic key ``"_missing"`` as a list of paths in input
    order. A caller that ignores ``_missing`` sees the prior
    shape and is forward-compatible; a caller that reads the
    key gets the new soft-signal class.

    A batch that yields *no* bodies at all (every path failed)
    raises ``ToolError("all reads failed")`` — the loud
    failure is preserved.
    """
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
        # One MCP session for the whole batch. Measured against the
        # live daemon: one-shot session p50 75.4 ms; persistent
        # session p50 41.9 ms per call (see I-9 numbers in
        # ``access.read_library_bodies``). A 20-path read drops
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
