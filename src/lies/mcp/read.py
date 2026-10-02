"""read MCP tool — verbatim page bodies via source-aware dispatch.

Replaces the old ``wiki_read`` tool. Dispatches:

- wiki page IDs (``page-…``) → ``memory_service.read()``
- library paths (``<collection>/<page>``) → the qmd daemon's ``get``

The library branch answers F19's citation contract, and that contract is
why the branch is a daemon call and not a CLI one. A citation is
``[[slug]]: "verbatim quote from the cited span"``, so the body this tool
returns has to be the document and nothing else. ``qmd get`` on the CLI
cannot supply that: it line-numbers every line by default, and
``--no-line-numbers`` still leaves its ``qmd://path  #docid`` header. The
daemon's ``get`` with ``lineNumbers: false`` is the only source of clean
text in qmd.

Two shapes of that answer, both load-bearing:

**The payload is a content block, not a string.** ``daemon_tool`` returns
the raw ``CallToolResult``; for both ``get`` and ``multi_get`` its
``.data`` is ``None`` and the text lives at ``.content[].resource.text``.
A caller that reaches for ``.data`` stores ``""`` — a page that reads as
empty, cited for nothing, with nothing in the response to say so. That is
the failure this module is shaped around, so :func:`_resource_texts` is
the only way the body is read here.

**Notices are not bodies.** ``multi_get`` reports per-file problems as
TextContent blocks: a ``[SKIPPED: …]`` line for a document over its 10KB
default cap, an ``Errors:`` block for an entry it could not resolve. A
caller that concatenates every block's text stores the notice as if it
were the page. :func:`_resource_texts` and :func:`_notices` keep the two
apart, and a result carrying notices but no resource block yields *no
body for that path* — never an empty string, which would reach the
synthesizer as a page with no content.

Why one ``get`` per path and not one ``multi_get`` per batch: the cap.
1854 of this corpus's 5987 documents are over ``multi_get``'s 10KB
default, and they arrive skipped rather than truncated — so batching
would silently drop nearly a third of what a reader can ask for, while
costing a round trip we do not need. ``get`` has no size cap.

Failure handling splits by who owns the failure, and only by that. A
document qmd cannot produce a body for — because the call raised, or
because it succeeded with nothing but notices — is logged and skipped
with its siblings intact. Both spellings of "no body for this path" are
treated identically on purpose: which one qmd chooses is an
implementation detail of its error signalling, not a fact about the
document, and branching on it would make a batch's outcome depend on
it. A daemon that is down or wedged is the one thing that re-raises.
When a batch genuinely yields nothing, ``ToolError("all reads failed")``
is the loud failure — raised once, at the end, rather than as a side
effect of the first bad page discarding every good body already
collected.

Known interaction: qmd's ``get`` prefixes ``<!-- Context: … -->`` when the
document's collection has a context configured. None of the registered
collections do, so no body carries one today; if that changes, the prefix
becomes part of the returned body and the citation contract has to be
re-checked.

Prior design (pre-daemon dispatch, still describes the tool surface and
the librarian's role):
``docs/superpowers/specs/2026-09-26-librarian-v040-port-design.md``.
Transport and body format are specified by the access seam
(``src/lies/qmd/access.py``) and this module's own contract above.
"""

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

#: Failures the daemon owns rather than the document. These re-raise;
#: everything else is a statement about one page and is skipped.
_DAEMON_FAILURES = (QmdDaemonUnavailable, QmdDaemonWedged)


def _memory_service() -> Any:
    """Lazy accessor for the wiki memory service.

    Returns a stub that raises if anyone actually tries to call it
    before the orchestrator wires a real service in. Production callers
    re-bind this attribute at module import time in Task 9.
    """
    raise RuntimeError("read._memory_service not wired")


def _resource_texts(result: Any) -> list[str]:
    """The document bodies in a qmd ``get`` / ``multi_get`` result.

    ``result.data`` is ``None`` on both tools — qmd answers with an
    EmbeddedResource content block. Reading ``.data`` is not a degraded
    read; it yields an empty body for a document that has content, and the
    empty body then reads downstream as "this page is empty".
    """
    blocks = getattr(result, "content", None) or []
    texts = []
    for block in blocks:
        text = getattr(getattr(block, "resource", None), "text", None)
        if isinstance(text, str):
            texts.append(text)
    return texts


def _notices(result: Any) -> list[str]:
    """The TextContent blocks in a qmd result — qmd's prose, never a body.

    ``multi_get`` reports a document over its size cap as
    ``[SKIPPED: <path> - …]`` and an unresolvable entry as
    ``Errors:\\nFile not found: <path>``, both as text blocks alongside the
    bodies. They are diagnostics about the read; treating one as a page is
    how a 315KB document comes back as a sentence saying it was skipped.
    """
    blocks = getattr(result, "content", None) or []
    return [t for t in (getattr(block, "text", None) for block in blocks) if isinstance(t, str)]


def _run_blocking(coro: Any) -> Any:
    """Drain a coroutine from sync code, with or without a running loop.

    ``_read_impl`` is sync — ten-odd existing tests, the
    ``Tool.from_function`` registration and ``server.py`` all assume it —
    while ``daemon_tool`` is async. FastMCP runs sync handlers in a
    threadpool and pydantic-ai runs sync tools in an executor, so both
    production call sites have no loop and ``asyncio.run`` is the whole
    story. ``lib_ask`` is async, though, and ``asyncio.run`` from inside a
    running loop raises ``RuntimeError: asyncio.run() cannot be called
    from a running event loop`` — the exact bug ``ground()`` shipped with
    (#106) and had to be unpicked afterwards.

    So the running-loop case gets its own thread and its own loop rather
    than an error. A thread is the honest answer here: the caller asked
    for a synchronous result, and blocking is the only way to produce one
    while the work needs an event loop that already exists elsewhere.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop in this thread — the ordinary case.
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="lies-read") as pool:
        return pool.submit(asyncio.run, coro).result()


def _daemon_body(path: str) -> str | None:
    """One verbatim body for ``path``, or ``None`` if qmd produced none.

    ``None`` covers both ways this read can come back empty: the call
    raised (``Document not found``), and the call succeeded but the
    result carried only notice blocks. Both are statements about *this
    document*, and both are reported the same way to the caller — logged
    and skipped — because the difference between them is an
    implementation detail of qmd's error signalling that this module
    does not control. Treating one as fatal and the other as skippable
    would make a batch's outcome depend on which channel qmd happened to
    use for the same fact.

    What is *not* skippable is a daemon that is down or wedged; the seam
    raises those before this function can, and the caller re-raises them.
    """
    try:
        result = _run_blocking(access.daemon_tool("get", {"file": path, "lineNumbers": False}))
    except _DAEMON_FAILURES:
        # The daemon is not serving, or accepted the call and stopped
        # answering. Neither is a statement about this document, and
        # swallowing either turns a reachable failure into "all reads
        # failed" — a claim about the corpus the operator is the only
        # person who can correct.
        raise
    except Exception as exc:
        log.warning("read: qmd get(%s) failed: %s", path, exc)
        return None

    bodies = _resource_texts(result)
    if not bodies:
        # The call succeeded and qmd still gave no document. Never
        # return "" here: an empty body reaches the synthesizer as a
        # page with no content, which reads downstream as "this page is
        # empty" and cites nothing.
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

    # The loud failure, raised once and only when the batch genuinely
    # produced nothing. Reaching here with an empty `out` means every
    # requested page failed, which is a different claim from "the caller
    # asked for one page and we did not have it" — and it is the claim
    # worth surfacing, since it is the one an operator can act on.
    if paths and not out:
        raise ToolError("all reads failed")

    return out


# Wrap as a FastMCP ``Tool`` so the MCP wire can serialize the dispatch
# surface and tests can reach the underlying function via ``read.fn(...)``.
# Mirrors the pattern in ``search.py`` (Task 4).
read = Tool.from_function(_read_impl, name="read")
