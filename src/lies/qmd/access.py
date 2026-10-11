"""The qmd access seam — every call LIES makes to qmd goes through here.

qmd exposes two surfaces that do not overlap. The MCP daemon serves
``query``, ``get``, ``multi_get``, ``status``; the CLI serves
everything else, including BM25 ``search`` (no daemon path).
Routing is by capability, never by availability.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

import fastmcp
import mcp  # type: ignore[import-not-found]
from fastmcp.client.transports import StreamableHttpTransport

from lies.config import get_qmd_url
from lies.qmd.daemon import (
    _is_daemon_stale,
    _reap_qmd_daemon,
    _spawn_qmd_daemon,
    recycle_qmd_daemon,
)
from lies.qmd.health import qmd_daemon_reachable
from lies.qmd.lock import qmd_lock_async, query_lock_wait_s

_log = logging.getLogger(__name__)

#: qmd's MCP daemon tools — the only operations it serves.
DAEMON_TOOLS: frozenset[str] = frozenset({"query", "get", "multi_get", "status"})

#: Everything the daemon cannot serve: index maintenance, lifecycle,
#: diagnostics, and BM25 ``search``.
CLI_ONLY_OPS: frozenset[str] = frozenset(
    {"search", "update", "embed", "cleanup", "ls", "doctor", "bench", "collection", "mcp"}
)

_PROBE_TIMEOUT_S = 0.5
_LOG_TAIL_BYTES = 2000


class QmdDaemonUnavailable(RuntimeError):
    """The qmd daemon is not serving. Message names the daemon URL and the
    command that starts it (``lies qmd up``, not ``lies mcp up``).
    """


class QmdDaemonWedged(RuntimeError):
    """The qmd daemon accepted the call and stopped answering.

    Carries ``last_output`` — the tail of qmd's own log at the wedge.
    """

    def __init__(self, message: str, *, last_output: str = "") -> None:
        super().__init__(message)
        self.last_output = last_output


#: Typed errors that mean "the daemon as a whole is in the wrong
#: state" — a per-path ``call_tool`` raised one of these, and the
#: whole session (and every path in it) must be abandoned. Anything
#: else is a per-path failure that siblings survive.
_DAEMON_FAILURES = (QmdDaemonUnavailable, QmdDaemonWedged)


def _daemon_client(url: str) -> fastmcp.Client:
    """A qmd client for ``url``, built fresh per call.

    Do NOT cache this. The requirement is that **no two calls share a
    client**, not that a client be exclusively owned while in use — a
    cached ``Client`` is a single object with a single MCP session
    behind it, and one session cannot serve concurrent calls. (The
    bullets below record which shapes did and did not reproduce that;
    call duration is an inference, not a measured threshold.)

    What is *not* the mechanism, because it was measured and is false
    for the installed fastmcp 4.0.10: ``Client._disconnect`` decrements
    ``nesting_counter`` and returns while it is still positive
    (`fastmcp/client/client.py:1080-1084`), so an exiting context does
    **not** stop a session a sibling is inside. Refcounted teardown is
    not what breaks. That behaviour is fastmcp-version-specific; do not
    assume it without reading the installed file.

    What the probes do support, and nothing stronger:

    - **Concurrent calls on one shared session can hang.** 2
      concurrent ``query`` gave ``MCPError: Connection closed``; 3 gave
      a hang then ``BrokenResourceError``.
    - **Fast calls did not reproduce it.** 3 concurrent ``status`` were
      all OK at ~0.1 s; 3 concurrent ``get`` were all OK; 1 ``query`` +
      3 ``get`` concurrently were all OK. So concurrency *alone* is not
      the trigger. Duration as the discriminator is an inference from
      those cases, not something a probe isolated — do not upgrade it
      to a measured threshold.
    - **Across event loops a shared client breaks under concurrency.**
      3 concurrent ``status`` calls, each on its own ``asyncio.run``
      loop: fresh per call **3/3 in 0.21 s**; one shared client
      **0/3**, unfinished at a 30 s box with the 20 s per-call timeout
      already expired (measured 2026-10-10, live daemon). So duration is
      irrelevant across loops -- these are ~0.2 s calls -- and no
      per-call deadline bounds the hang. Control: the same shared client
      driven by three loops *one at a time* is 3/3 ok, so the failure
      needs cross-loop concurrency, not mere cross-loop reuse.
      Structurally, ``ClientSessionState.lock`` is an ``anyio.Lock``
      built in ``Client.__init__``
      (``fastmcp/client/client.py:228`` via ``:587``) and ``anyio.Lock``
      binds its backend at construction
      (``anyio/_core/_synchronization.py:164-168``), so a cached client
      carries a loop-A lock into loop B -- a hazard to design around,
      but not by itself a proven cause, since sequential cross-loop use
      works. Those line numbers are for the installed fastmcp and will
      drift with the version.

    Headline, measured 2026-10-10 against the live daemon and
    transcribed (not re-measured at commit time): 3 concurrent ``search``
    calls gave **0/3 complete at 60.1 s** with the cache and **3/3 in
    1.4 s** without. Load envelope, all clean: N=6 6/6 in 2.6 s; N=9
    9/9 in 3.2 s; N=15 15/15 in 7.2 s; N=25 25/25 in 8.3 s; and 5
    search + 5 read mixed 10/10 in 2.53 s.

    Nothing is given up by rebuilding. The handshake already happened
    per call either way, because ``_call`` opened a session per call
    under the cache too — so this costs +0.9 ms / +1 % on 20 ``status``
    calls (median 69.1 ms shared, 70.0 ms fresh). And the reason the
    cache claimed to exist is false: the embedding model lives in the
    *qmd daemon*, whose RSS stayed flat at 1957-1965 MiB across every
    run; LIES' own RSS was ~182 MiB either way and VRAM moved 0 MiB.
    """
    # Lazy: `lies.qmd.mcp` imports `classify_call_error` from here, so a
    # top-level import would close a cycle.
    from lies.qmd.mcp import _build_qmd_httpx_client

    return fastmcp.Client(
        StreamableHttpTransport(url, httpx_client_factory=_build_qmd_httpx_client)
    )


# --- the taxonomy ------------------------------------------------------
#
# Measured against fastmcp 4.0.3 (httpx2-vendored, classes checked by
# *name*). A dead session arrives as a bare ``RuntimeError`` with the
# real error on ``__cause__``; ``_transport_cause`` follows the chain.

#: Names that mean "the daemon accepted the request and stopped answering".
#: ``PoolTimeout`` is excluded: a pool timeout means LIES' own
#: concurrency exhausted the httpx connection pool (a client-side
#: state, not a daemon-stalling signal), and ``recycle-raise`` on a
#: machine-global daemon would kill in-flight work belonging to
#: other clients. ``PoolTimeout`` keeps the ``recycle-retry`` path
#: through ``_TRANSPORT_NAMES`` (its MRO walks through
#: ``TimeoutException``).
_WEDGE_NAMES = frozenset({"ReadTimeout", "WriteTimeout"})

#: Names that mean "the daemon was not there".
_TRANSPORT_NAMES = frozenset({"TransportError", "TimeoutException", "ConnectError"})

#: Names that mean the *client* sent something the server rejected.
#: ``LocalProtocolError`` is httpx's name for an illegal header value,
#: unsupported URL scheme, or otherwise malformed request — a retry
#: is provably futile (the same bytes will fail the same way) and a
#: recycle kills in-flight work belonging to other clients of the
#: machine-global daemon. Treat as ``passthrough`` like the
#: server-state ``HTTPStatusError``. ``RemoteProtocolError`` keeps
#: the ``recycle-retry`` path: the daemon's response was malformed,
#: which is a server-side state.
_LOCAL_PROTOCOL_NAMES = frozenset({"LocalProtocolError"})

#: JSON-RPC code for session death under an in-flight call. Literal
#: because the SDK constant moved between releases.
_CONNECTION_CLOSED = -32000


def _is_wedge(exc: BaseException) -> bool:
    """True for a read/write/pool timeout, in either httpx generation."""
    for klass in type(exc).__mro__:
        if klass.__name__ in _WEDGE_NAMES:
            return True
    return False


def _is_transport(exc: BaseException) -> bool:
    """True for any transport-level failure, in either httpx generation."""
    return any(klass.__name__ in _TRANSPORT_NAMES for klass in type(exc).__mro__)


def _is_local_protocol_error(exc: BaseException) -> bool:
    """True for a client-side malformed-request protocol error."""
    return any(klass.__name__ in _LOCAL_PROTOCOL_NAMES for klass in type(exc).__mro__)


def _transport_cause(exc: BaseException) -> Exception | None:
    """The transport error underneath fastmcp's dead-session wrapper.

    Follow the ``__cause__`` chain for a transport failure, or
    ``None``. ``BaseException`` not derived from ``Exception`` ends the
    walk.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, Exception) and (_is_wedge(current) or _is_transport(current)):
            return current
        current = current.__cause__
    return None


def classify_call_error(exc: Exception) -> tuple[str, bool]:
    """Classify a failed daemon call as ``(action, retryable)``.

    ``action``:

    - ``"recycle-raise"`` — wedged. Restart, then raise.
    - ``"recycle-retry"`` — unreachable. Restart and try once more.
    - ``"passthrough"`` — not a transport failure. Propagates.

    Order matters: a read timeout is *also* a transport error, so the
    wedge case is decided first. A connect timeout is a transport
    error and not a wedge. ``httpx.HTTPStatusError`` and
    ``httpx.LocalProtocolError`` are ``"passthrough"`` — server-side
    status and client-side malformed requests both leave the daemon
    reachable and are not reasons to recycle a machine-global daemon.
    ``httpx.RemoteProtocolError`` keeps ``"recycle-retry"`` (the
    daemon's response was malformed — server-side state).

    Args:
        exc: Class name and ``__cause__`` chain — httpx types are not.

    Returns:
        ``(action, retryable)``.
    """
    if _is_wedge(exc):
        return ("recycle-raise", False)
    if _is_local_protocol_error(exc):
        return ("passthrough", False)
    if _is_transport(exc):
        return ("recycle-retry", True)
    cause = _transport_cause(exc)
    if cause is not None:
        return classify_call_error(cause)
    if isinstance(exc, mcp.MCPError):
        if exc.code == _CONNECTION_CLOSED:
            return ("recycle-raise", False)
        if exc.code == httpx.codes.REQUEST_TIMEOUT:
            return ("recycle-retry", True)
    return ("passthrough", False)


async def qmd_collection_names() -> frozenset[str]:
    """The collection set the daemon is currently serving.

    Reads the daemon's ``status`` tool. No cache: there is no
    daemon-side invalidation signal, and LIES' own ingest paths do
    not clear it. Measured ~42ms / call — under 1% of a 5.6–6.0s
    warm search.

    Returns:
        Frozen set of names the daemon reports serving. Empty when
        the structured ``collections`` field is absent or malformed
        (the same permissive shape both ``list[str]`` and ``list[dict]``
        are accepted, so a daemon schema change does not lock the
        caller).

    Raises:
        QmdDaemonUnavailable: the daemon is not serving.
        QmdDaemonWedged: the daemon accepted the call and stopped
            answering.
    """
    result = await daemon_tool("status", {})
    structured = getattr(result, "structured_content", None) or {}
    rows = structured.get("collections") or []
    if not isinstance(rows, list):
        return frozenset()
    names: set[str] = set()
    for row in rows:
        # Both shapes read so the schema choice does not lock the helper.
        if isinstance(row, str):
            if row:
                names.add(row)
        elif isinstance(row, dict):
            name = row.get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return frozenset(names)


async def validate_scope(scope: list[str]) -> tuple[list[str], list[str]]:
    """Validate ``scope`` against the daemon's binding collection set.

    The daemon answers an unknown collection with an empty result and
    **no error** (the CLI exits 1 on the same class), so a batched
    query carrying one unresolvable name silently returns zero rows.
    Pre-validation surfaces the absent name before the call so the
    caller can distinguish "you named something the daemon doesn't
    serve" from "the corpus had no in-scope hits".

    Args:
        scope: Collection names drawn from the LIES registry (or any
            caller-side resolver).

    Returns:
        ``(validated, unknown)``. ``validated`` is the subset of
        ``scope`` that the daemon reports serving, in input order.
        ``unknown`` is the names in ``scope`` but absent from the
        daemon's ``status`` (also in input order). The two lists are
        disjoint and ``len(validated) + len(unknown) == len(scope)``.

    Raises:
        QmdDaemonUnavailable: the daemon is not serving.
        QmdDaemonWedged: the daemon accepted the call and stopped
            answering.
    """
    if not scope:
        return [], []
    served = await qmd_collection_names()
    validated: list[str] = []
    unknown: list[str] = []
    for name in scope:
        if name in served:
            validated.append(name)
        else:
            unknown.append(name)
    return validated, unknown


async def daemon_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Any:
    """Call one qmd daemon tool, recovering per the taxonomy.

    ``query`` alone is single-flighted on the site-wide qmd lock. The
    daemon runs queries strictly one at a time — three concurrent ones
    measured a 0.99x speedup, the second waiting for the first and the
    third for both — so an ungated caller is not parallelising, it is
    joining an unbounded queue inside another process, where nothing
    bounds it, nothing logs it and nothing can be waited on. Inside qmd
    that queue is invisible; from out here it arrives as a call that took
    too long. The gate moves the wait to a place with a budget
    (:func:`query_lock_wait_s`) and an owner.

    **There is no log line on the wait path.** ``_acquire_with_poll_async``
    writes nothing while it polls, and a 2 s blocked ``search`` added 12
    lines to ``qmd/mcp.log`` — all transport plumbing, none about the
    lock — because the wait never reaches the daemon; the caller is
    blocked on a local inode. What survives is the ephemeral ``.pid``
    sidecar beside the lock and the ``fallback_reason`` the exception's
    fields are copied into on the ask path.

    ``get``, ``multi_get`` and ``status`` stay unlocked. All three answer
    in milliseconds, and :func:`read_library_bodies` issues one ``get`` per
    path, so gating them would park a whole batch behind the slowest
    query in the process and buy nothing — the queue they would join is
    one long query, not each other.

    Args:
        name: A member of :data:`DAEMON_TOOLS`. Anything else raises.
        arguments: The tool's arguments, forwarded verbatim.
        timeout: Per-call read deadline in seconds, or ``None``.

    Returns:
        The raw ``CallToolResult``; ``.content`` for ``get``/``multi_get``.

    Raises:
        ValueError: not a daemon tool. QmdDaemonUnavailable: not serving.
        QmdDaemonWedged: stopped answering. RuntimeError: tool error.
        QmdLockBusy: a ``query`` found the lock held past its wait budget.
            Deliberately *not* a daemon failure — the daemon is answering
            fine, something else holds the slot, and the remedy is to wait
            and retry rather than to restart anything.
    """
    if name == "query":
        async with qmd_lock_async(timeout_s=query_lock_wait_s()):
            return await _call_daemon_tool(name, arguments, timeout=timeout)
    return await _call_daemon_tool(name, arguments, timeout=timeout)


async def _call_daemon_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Any:
    """One daemon tool call: validate, probe, reap if stale, call, recover.

    The body of :func:`daemon_tool`, split out so the query gate wraps a
    named thing rather than the whole function. That split also decides
    where the reachability probe sits: **inside** the gate, deliberately.
    A probe in front of it would report a down daemon as
    ``QmdLockBusy`` whenever any holder happened to be live, which
    points the operator at the wrong remedy — wait and retry, when the
    one that works is ``lies qmd up``. The caller should learn the
    daemon is absent, not that it is busy.

    Args:
        name: A member of :data:`DAEMON_TOOLS`. Anything else raises.
        arguments: The tool's arguments, forwarded verbatim.
        timeout: Per-call read deadline in seconds, or ``None``.

    Returns:
        The raw ``CallToolResult``.

    Raises:
        ValueError: not a daemon tool. QmdDaemonUnavailable: not serving.
        QmdDaemonWedged: stopped answering. RuntimeError: tool error.
    """
    if name not in DAEMON_TOOLS:
        raise ValueError(
            f"{name!r} is not a qmd daemon tool (CLI_ONLY: {sorted(CLI_ONLY_OPS)}); "
            f"the daemon serves {sorted(DAEMON_TOOLS)}"
        )

    url = get_qmd_url()
    if not qmd_daemon_reachable(url, timeout=_PROBE_TIMEOUT_S):
        raise QmdDaemonUnavailable(
            f"qmd daemon is not serving at {url}. Start it with 'lies qmd up', "
            f"or point LIES_QMD_URL at a daemon that is."
        )

    if _is_daemon_stale():
        _log.info("qmd daemon stale; reaping before the call")
        _reap_qmd_daemon()
        _spawn_qmd_daemon()

    client = _daemon_client(url)
    return await _call_with_recovery(
        url, name, lambda: _call(client, name, arguments, timeout=timeout)
    )


async def _call_with_recovery(
    url: str,
    name: str,
    make_call: Callable[[], Awaitable[Any]],
) -> Any:
    """Run ``make_call`` with the taxonomy applied to any failure.

    The single place a daemon failure is classified. Every daemon
    call site routes through here: a site that calls
    ``client.call_tool`` directly bypasses the taxonomy, and a
    transport failure it swallows becomes a per-path "not in the
    corpus" — a fact about the process reported as a fact about
    the index.

    Args:
        url: Daemon URL, for the recycle and the raise text.
        name: Tool name, for the raise text.
        make_call: Zero-argument callable returning the awaitable.
            Invoked once, and a second time only after a recycle on
            a retryable failure.

    Returns:
        Whatever ``make_call`` returns.

    Raises:
        QmdDaemonWedged: the daemon accepted the call and stopped
            answering, before or after a recycle.
        QmdDaemonUnavailable: not serving, or still not serving after
            one recycle-and-retry.
        Exception: re-raised unchanged for a ``"passthrough"`` class,
            and for a second failure the taxonomy does not own.
    """
    try:
        return await make_call()
    except Exception as exc:
        action, retryable = classify_call_error(exc)
        if action == "passthrough":
            raise
        if not retryable:
            # Wedge branch: read the daemon's log *before* the recycle.
            # qmd truncates mcp.log on every daemon start.
            last_output = _daemon_log_tail()
            recycled = await _recycle_or_report(url)
            outcome = (
                "recycled, but the same payload is not retried against a "
                "daemon that re-wedges on it"
                if recycled
                else f"the restart also failed, so {_DAEMON_DOWN_HINT}"
            )
            raise QmdDaemonWedged(
                f"qmd daemon wedged on call to {name!r}; {outcome}",
                last_output=last_output,
            ) from exc
        await _recycle_or_report(url)
        try:
            return await make_call()
        except Exception as retry_exc:
            retry_action = classify_call_error(retry_exc)[0]
            if retry_action == "passthrough":
                raise
            if retry_action == "recycle-raise":
                # The fresh daemon wedged; reading here puts its own
                # last words on the exception.
                raise QmdDaemonWedged(
                    f"qmd daemon wedged again on call to {name!r} after a recycle",
                    last_output=_daemon_log_tail(),
                ) from retry_exc
            raise QmdDaemonUnavailable(
                f"qmd daemon still not serving at {url} after a recycle "
                f"({retry_exc}); start it with 'lies qmd up', or point "
                f"LIES_QMD_URL at a daemon that is."
            ) from retry_exc


async def _recycle_or_report(url: str) -> bool:
    """Recycle, reporting rather than propagating a failed restart.

    Returns ``True`` when a replacement served. The caller keeps its
    own diagnosis -- a wedge stays a wedge -- and appends the
    operator-actionable fact when this returns ``False``. A bare
    ``await _recycle(url)`` would replace the diagnosis with a
    recycle failure, which is the *consequence*, not the cause.
    """
    try:
        await _recycle(url)
    except Exception:
        # **This breadth is load-bearing; do not narrow it to
        # ``QmdRecycleFailed``.** Before the recycle's probe tuple was
        # widened it escaped with an undocumented bare ``RuntimeError``
        # ("Client failed to connect: ...") that is not a
        # ``QmdRecycleFailed`` and never was one — a ``QmdRecycleFailed``
        # -only catch would have let it through here. Post-fix,
        # ``recycle_qmd_daemon``'s contract is the single
        # ``QmdRecycleFailed``, so this catch is defensive only; it stays
        # broad because the failure it was absorbing was not one of ours
        # to name, and because the wedge branch below relies on this
        # returning False rather than replacing the caller's diagnosis.
        return False
    return True


async def _call(
    client: fastmcp.Client,
    name: str,
    arguments: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Any:
    """One tool call, with the daemon's own error text surfaced.

    ``raise_on_error=False`` so a tool error arrives as a result.

    ``client`` must not be shared with a concurrent call — that, not
    exclusive ownership while the ``async with`` is open, is what
    :func:`_daemon_client` buys by building one per call. A shared
    client's session cannot serve concurrent calls, and its
    ``anyio.Lock`` binds to the loop that opened it (see
    ``_daemon_client``). That docstring carries the measurements, which
    recorded shapes, not a duration threshold, plus the
    fastmcp-version-specific refcounting behaviour that is *not* the
    mechanism.
    """
    async with client:
        if timeout is None:
            result = await client.call_tool(name, arguments, raise_on_error=False)
        else:
            result = await client.call_tool(name, arguments, raise_on_error=False, timeout=timeout)
    if getattr(result, "is_error", False):
        raise RuntimeError(f"qmd tool {name!r} failed: {_result_text(result)}")
    return result


def _get_in_session(
    client: fastmcp.Client,
    path: str,
    *,
    timeout: float | None,
) -> Awaitable[Any]:
    """The per-path ``get`` inside an already-open session.

    Separate from :func:`_call` because that helper opens and closes
    the session; the batched read holds one open for the whole
    batch.

    ``raise_on_error=False`` — qmd's "no body for this path" arrives
    as a result shape, not an exception, which is what lets the
    batch distinguish a missing document from a transport failure.
    """
    args: dict[str, Any] = {"file": path, "lineNumbers": False}
    if timeout is None:
        return client.call_tool("get", args, raise_on_error=False)
    return client.call_tool("get", args, raise_on_error=False, timeout=timeout)


async def read_library_bodies(
    paths: list[str],
    *,
    timeout: float | None = None,
) -> list[Any]:
    """Issue one ``get`` per path over a single MCP session.

    Hoists the MCP session handshake out of the per-path loop
    measured against the live daemon (2026-10-03, 10 warm
    samples, ``claude_code/concepts/hooks.md``): one-shot session
    p50 75.4 ms; persistent-session ``call_tool`` p50 41.9 ms.
    The ~33 ms per-session handshake was 40 % of a 20-path read;
    a hoisted bridge saves ~600 ms per 20-path read while
    preserving the one-``get``-per-path decision
    (``multi_get`` silently skips 10KB+ documents — 1854 of this
    corpus's 5987 are over the cap).

    That session is per *batch* and is never shared with another batch
    or with a concurrent call. Not because an exit would tear it out from
    under a sibling — fastmcp 4.0.10 refcounts that — but because one
    session cannot serve concurrent calls and its ``anyio.Lock`` binds
    to the loop that opened it. See :func:`_daemon_client`.

    Args:
        paths: Library paths (``<collection>/<page>``) to fetch.
        timeout: Per-call read deadline in seconds.

    Returns:
        List of ``CallToolResult`` in input order. Each entry is
        one path's result; ``is_error=True`` on tool-side errors,
        ``.content`` carrying the body or the skip/error notice.
        A per-path failure maps to ``None`` in the output list so
        siblings survive. The two channels qmd actually uses for
        "no body for this path" are result shapes, not
        exceptions: an ``is_error`` result, and a notice-only
        result. Both are ``None`` entries. A transport failure is
        neither — it goes through the taxonomy and propagates.

    Raises:
        QmdDaemonUnavailable: the daemon is not serving (operator action).
        QmdDaemonWedged: the session wedge — the whole batch is
            re-raised against the parent tool, which decides.
    """
    if not paths:
        return []
    url = get_qmd_url()
    if not qmd_daemon_reachable(url, timeout=_PROBE_TIMEOUT_S):
        raise QmdDaemonUnavailable(
            f"qmd daemon is not serving at {url}. Start it with 'lies qmd up', "
            f"or point LIES_QMD_URL at a daemon that is."
        )

    client = _daemon_client(url)
    out: list[Any] = []
    async with client:
        for path in paths:
            try:
                result = await _call_with_recovery(
                    url,
                    "get",
                    lambda p=path: _get_in_session(client, p, timeout=timeout),
                )
            except _DAEMON_FAILURES:
                raise
            except Exception as exc:
                # The taxonomy owns every transport failure, so what
                # reaches here is a ``"passthrough"`` class: the
                # daemon is serving and this one request was bad.
                # That is genuinely per-path, so siblings survive.
                # A bug raised here (a ``TypeError`` from a bad
                # argument) is not — see the test that pins a
                # transport error to a raise rather than a ``None``.
                _log.warning("read: qmd get(%s) failed per-path: %s", path, exc)
                out.append(None)
                continue
            if getattr(result, "is_error", False):
                out.append(None)
                continue
            out.append(result)
    return out


def _result_text(result: Any) -> str:
    """The text blocks of a call result, joined."""
    blocks = getattr(result, "content", None) or []
    return " ".join(getattr(block, "text", "") for block in blocks).strip()


def _recycle_scope() -> Path:
    """The LIES scope to record for a daemon started by :func:`_recycle`.

    Always ``library_git_root()``. This used to be "the sidecar's value,
    else ``library_git_root()``", which is a fixed point: it perpetuates
    whichever value ran last, including a stale one after a wiki moves.
    The sidecar is a *record* of the last session, so reading it back to
    decide the next one makes the record self-confirming and unfalsifiable.

    It is also not an index. ``_spawn_qmd_daemon``'s ``data_dir``
    argument is the only thing here that selects one, and it is
    deliberately not fed from this — qmd resolves its own index from the
    environment the reap-then-spawn inherits. See
    ``daemon._canonical_scope`` for the three call sites that used to
    disagree, and why two of them differed by construction.
    """
    from lies.library.registry import library_git_root

    return library_git_root()


#: Operator instruction attached whenever a recycle leaves the daemon
#: stopped. A machine-global daemon that is not running is an
#: operator action, and the one action that resolves it.
_DAEMON_DOWN_HINT = (
    "the qmd daemon is DOWN and must be started with 'lies qmd up' "
    "(or point LIES_QMD_URL at a daemon that is running)"
)


async def _recycle(url: str) -> None:
    """Restart the daemon. Raises when the replacement never served.

    Raising is the point. A recycle that exhausts its budget has
    stopped a machine-global daemon and left it stopped, and the only
    remedy is an operator starting one -- so the fact has to reach
    both the log and the caller. This used to log a warning and
    return, which meant the caller's error said "recycled" about a
    restart that did not happen and named no command; every qmd call
    on this host then failed with a wedge nobody could act on.
    """
    from lies.qmd.daemon import QmdRecycleFailed

    try:
        state = await recycle_qmd_daemon(scope=_recycle_scope(), daemon_url=url)
    except QmdRecycleFailed as exc:
        _log.error(
            "qmd recycle exhausted ready_timeout=%gs; last_state=%s; %s",
            exc.ready_timeout_s,
            exc.last_state.detail,
            _DAEMON_DOWN_HINT,
        )
        raise
    _log.info("qmd daemon recycled (pid=%s); reason=%s", state.pid, state.detail)


def _daemon_log_tail() -> str:
    """The tail of qmd's own daemon log — evidence a wedge needs."""
    from lies.qmd.lifecycle import _logfile

    try:
        with _logfile().open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
