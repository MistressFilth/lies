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
    daemon_port_from_url,
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
    """Returns a client for the qmd daemon at ``url``, built fresh per call.

    Callers must not share the returned client across concurrent calls:
    one ``Client`` carries one MCP session, and a session cannot serve
    overlapping calls. Measurements are in AGENTS.md.
    """
    # Lazy: `lies.qmd.mcp` imports `classify_call_error` from here, so a
    # top-level import would close a cycle.
    from lies.qmd.mcp import _build_qmd_httpx_client

    return fastmcp.Client(
        StreamableHttpTransport(url, httpx_client_factory=_build_qmd_httpx_client)
    )


# --- the taxonomy ------------------------------------------------------
#
# Matched by exception class *name*, not by httpx type: fastmcp vendors
# its own httpx, so an LIES-side type check matches none of them.

#: Names that mean "the daemon accepted the request and stopped answering".
#: ``PoolTimeout`` is excluded on purpose: it means LIES' own concurrency
#: exhausted the httpx pool (client-side), and ``recycle-raise`` on a
#: machine-global daemon would kill other clients' in-flight work. It
#: keeps the ``recycle-retry`` path via ``_TRANSPORT_NAMES``.
_WEDGE_NAMES = frozenset({"ReadTimeout", "WriteTimeout"})

#: Names that mean "the daemon was not there".
_TRANSPORT_NAMES = frozenset({"TransportError", "TimeoutException", "ConnectError"})

#: Names that mean the *client* sent something the server rejected.
#: ``LocalProtocolError`` is httpx's name for a malformed request; a retry
#: is provably futile (the same bytes fail the same way). Treat as
#: ``passthrough``. ``RemoteProtocolError`` is server-side and keeps the
#: ``recycle-retry`` path.
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
    """Classifies a failed daemon call as ``(action, retryable)``.

    ``action`` is ``"recycle-raise"`` (wedged), ``"recycle-retry"``
    (unreachable) or ``"passthrough"``. Order matters: a read timeout is
    also a transport error, so the wedge case is decided first.

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

    Reads the daemon's ``status`` tool; there is no daemon-side
    invalidation signal, so nothing is cached.

    Returns:
        Frozen set of names the daemon reports serving; empty when the
        structured ``collections`` field is absent or malformed.

    Raises:
        QmdDaemonUnavailable: The daemon is not serving.
        QmdDaemonWedged: The daemon accepted the call and stopped answering.
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
    """Validates ``scope`` against the daemon's binding collection set.

    The daemon answers an unknown collection with an empty result and
    no error, so a batched query carrying one unresolvable name silently
    returns zero rows.

    Args:
        scope: Collection names drawn from the LIES registry or any resolver.

    Returns:
        ``(validated, unknown)`` — the served subset and the absent
        names, both in input order and mutually disjoint.

    Raises:
        QmdDaemonUnavailable: The daemon is not serving.
        QmdDaemonWedged: The daemon accepted the call and stopped answering.
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
    """Calls one qmd daemon tool, recovering per the taxonomy.

    ``query`` alone is single-flighted on the site-wide qmd lock. qmd
    executes queries strictly one at a time, so an ungated caller joins
    an unbounded queue inside another process where nothing bounds it.
    The gate moves that wait to a place with a budget
    (:func:`query_lock_wait_s`) and an owner. ``get``, ``multi_get`` and
    ``status`` stay unlocked: they answer in milliseconds, and gating
    them would park a whole batch behind the slowest query in the
    process.

    Args:
        name: A member of :data:`DAEMON_TOOLS`; anything else raises. ``arguments`` is
            forwarded verbatim and ``timeout`` is the read deadline in seconds.

    Returns:
        The raw ``CallToolResult``; ``.content`` for ``get``/``multi_get``.

    Raises:
        ValueError: Not a daemon tool.
        QmdDaemonUnavailable: Not serving.
        QmdDaemonWedged: Stopped answering.
        RuntimeError: Tool error.
        QmdLockBusy: A ``query`` found the lock held past its wait budget.
            Deliberately not a daemon failure — the remedy is to wait and
            retry, not to restart anything.
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

    Split out of :func:`daemon_tool` so the query gate wraps a named
    thing. The reachability probe sits *inside* the gate: in front of it
    a down daemon would surface as ``QmdLockBusy`` and send the
    operator to wait when the remedy is ``lies qmd up``.

    Raises:
        ValueError: Not a daemon tool.
        QmdDaemonUnavailable: Not serving.
        QmdDaemonWedged: Stopped answering.
        RuntimeError: Tool error.
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
        # Spawn onto the port this call is about to use, or the reap
        # kills the configured daemon and the call proceeds against one
        # that is no longer there.
        _spawn_qmd_daemon(port=daemon_port_from_url(url))

    client = _daemon_client(url)
    return await _call_with_recovery(
        url, name, lambda: _call(client, name, arguments, timeout=timeout)
    )


async def _call_with_recovery(
    url: str,
    name: str,
    make_call: Callable[[], Awaitable[Any]],
) -> Any:
    """Runs ``make_call`` with the taxonomy applied to any failure.

    The single place a daemon failure is classified; a site that calls
    ``client.call_tool`` directly bypasses it.

    Args:
        url: Daemon URL, for the recycle and the raise text.
        name: Tool name, for the raise text.
        make_call: Zero-argument callable returning the awaitable.
            Invoked once, and a second time only after a recycle on a
            retryable failure.

    Returns:
        Whatever ``make_call`` returns.

    Raises:
        QmdDaemonWedged: The daemon accepted the call and stopped
            answering, before or after a recycle.
        QmdDaemonUnavailable: Not serving, or still not serving after one
            recycle-and-retry.
        Exception: Re-raised unchanged for a ``"passthrough"`` class, and
            for a second failure the taxonomy does not own.
    """
    try:
        return await make_call()
    except Exception as exc:
        action, retryable = classify_call_error(exc)
        if action == "passthrough":
            raise
        if not retryable:
            # Read the daemon's log *before* the recycle; qmd truncates
            # mcp.log on every start.
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
                # The fresh daemon wedged; read its own last words.
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
    """Recycles, reporting rather than propagating a failed restart.

    Returns ``True`` when a replacement served; the caller keeps its own
    diagnosis. The catch is deliberately broader than
    :class:`~lies.qmd.daemon.QmdRecycleFailed`, whose own probe can
    surface a bare ``RuntimeError`` from a failed connect.
    """
    try:
        await _recycle(url)
    except Exception:
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
    :func:`_daemon_client` buys by building one per call.
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

    Separate from :func:`_call` because the batched read holds one
    session open for the whole batch. ``raise_on_error=False`` — qmd's
    "no body for this path" arrives as a result shape, not an exception.
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
    """Issues one ``get`` per path over a single MCP session.

    Hoists the handshake out of the per-path loop. One ``get`` per path
    is deliberate: ``multi_get`` silently skips 10KB+ documents.

    Args:
        paths: Library paths (``<collection>/<page>``); ``timeout`` is the read deadline.

    Returns:
        One entry per path, in input order: the ``CallToolResult``, or
        ``None`` for a per-path failure so its siblings survive.

    Raises:
        QmdDaemonUnavailable: The daemon is not serving.
        QmdDaemonWedged: The session wedged; the parent tool decides.
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
                # What reaches here is a ``"passthrough"`` class: the
                # daemon is serving and this one request was bad.
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

    Always ``library_git_root()``. Reading the sidecar's value back
    would make the record self-confirming and perpetuate a stale path
    after a wiki moves. The value is a scope, not an index; see
    ``daemon._canonical_scope``.
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
    """Restarts the daemon. Raises when the replacement never served.

    Raising is the point: a recycle that exhausts its budget has
    stopped a machine-global daemon, and the only remedy is an operator
    starting one.
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
