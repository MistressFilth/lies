"""The qmd access seam — every call LIES makes to qmd goes through here.

qmd exposes two surfaces that do not overlap. The MCP daemon serves
``query``, ``get``, ``multi_get``, ``status``; the CLI serves
everything else, including BM25 ``search`` (no daemon path).
Routing is by capability, never by availability.
"""

from __future__ import annotations

import logging
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
    read_sidecar_data_dir,
    recycle_qmd_daemon,
)
from lies.qmd.health import qmd_daemon_reachable

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


# Cached per process so the daemon's model is not re-warmed per call.
# An MCP session is bound to the event loop that opened it, and this
# seam is called from `asyncio.run` bridges that each get a fresh loop.
_client: fastmcp.Client | None = None
_client_url: str | None = None


def _daemon_client(url: str) -> fastmcp.Client:
    """The process-wide qmd client for ``url``, built on first use."""
    global _client, _client_url
    if _client is not None and _client_url == url:
        return _client
    # Lazy: `lies.qmd.mcp` imports `classify_call_error` from here, so a
    # top-level import would close a cycle.
    from lies.qmd.mcp import _build_qmd_httpx_client

    _client = fastmcp.Client(
        StreamableHttpTransport(url, httpx_client_factory=_build_qmd_httpx_client)
    )
    _client_url = url
    return _client


def _reset_client() -> None:
    """Drop the cached client. For tests and for a changed daemon URL."""
    global _client, _client_url
    _client = None
    _client_url = None


# --- the taxonomy ------------------------------------------------------
#
# Measured against fastmcp 4.0.3 (httpx2-vendored, classes checked by
# *name*). A dead session arrives as a bare ``RuntimeError`` with the
# real error on ``__cause__``; ``_transport_cause`` follows the chain.

#: Names that mean "the daemon accepted the request and stopped answering".
_WEDGE_NAMES = frozenset({"ReadTimeout", "WriteTimeout", "PoolTimeout"})

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

    Args:
        name: A member of :data:`DAEMON_TOOLS`. Anything else raises.
        arguments: The tool's arguments, forwarded verbatim.
        timeout: Per-call read deadline in seconds, or ``None``.

    Returns:
        The raw ``CallToolResult``; ``.content`` for ``get``/``multi_get``.

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
    try:
        return await _call(client, name, arguments, timeout=timeout)
    except Exception as exc:
        action, retryable = classify_call_error(exc)
        if action == "passthrough":
            raise
        if not retryable:
            # Wedge branch: read the daemon's log *before* the recycle.
            # qmd truncates mcp.log on every daemon start.
            last_output = _daemon_log_tail()
            await _recycle(url)
            raise QmdDaemonWedged(
                f"qmd daemon wedged on call to {name!r}; recycled, but the same "
                f"payload is not retried against a daemon that re-wedges on it",
                last_output=last_output,
            ) from exc
        await _recycle(url)
        try:
            return await _call(client, name, arguments)
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


async def _call(
    client: fastmcp.Client,
    name: str,
    arguments: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Any:
    """One tool call, with the daemon's own error text surfaced.

    ``raise_on_error=False`` so a tool error arrives as a result.
    """
    async with client:
        if timeout is None:
            result = await client.call_tool(name, arguments, raise_on_error=False)
        else:
            result = await client.call_tool(name, arguments, raise_on_error=False, timeout=timeout)
    if getattr(result, "is_error", False):
        raise RuntimeError(f"qmd tool {name!r} failed: {_result_text(result)}")
    return result


def _result_text(result: Any) -> str:
    """The text blocks of a call result, joined."""
    blocks = getattr(result, "content", None) or []
    return " ".join(getattr(block, "text", "") for block in blocks).strip()


def _recycle_data_dir() -> Path:
    """The ``data-dir`` to record for a daemon started by :func:`_recycle`.

    Sidecar first, library root fallback. ``_spawn_qmd_daemon`` never
    reads its ``data_dir`` (``daemon.py:252-261``), so the disagreement
    is only what gets recorded.
    """
    recorded = read_sidecar_data_dir()
    if recorded is not None:
        return recorded
    from lies.library.registry import library_git_root

    return library_git_root()


async def _recycle(url: str) -> None:
    """Restart the daemon. A recycle that never served is logged, not raised."""
    from lies.qmd.daemon import QmdRecycleFailed

    try:
        state = await recycle_qmd_daemon(data_dir=_recycle_data_dir(), daemon_url=url)
    except QmdRecycleFailed as exc:
        _log.warning(
            "qmd recycle exhausted ready_timeout=%gs; last_state=%s",
            exc.ready_timeout_s,
            exc.last_state.detail,
        )
        return
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
