"""The qmd access seam — every call LIES makes to qmd goes through here.

qmd exposes two surfaces and they do not overlap. Its MCP daemon serves
``query``, ``get``, ``multi_get``, and ``status``; its CLI serves
everything else — ``update``/``embed``/``cleanup``, the collection and
daemon lifecycle verbs, ``doctor``/``bench``/``ls``, and BM25
``search``, which the daemon has no path for at all. The two constants
below are that split, and they are the reason the seam exists: one
module knows which transport serves which operation, so no call site has
to decide, and no call site can quietly pick the wrong one.

Routing is by capability, never by availability
------------------------------------------------

A down daemon raises :class:`QmdDaemonUnavailable` naming the fix, and
the fix is ``lies qmd up`` — not ``lies mcp up``, which starts LIES' own
MCP server and would leave the operator exactly where they were. There
is no degraded mode, no empty result, no falling back to the CLI. The
previous behaviour mapped an unreachable daemon to "no relevant content
in the library" — a claim about the corpus that was really a claim about
the process, and the operator who had to act was never told.

A wedged daemon — one that accepted the connection and then stopped
answering — is a different failure from a down one and gets a different
answer: recycle, then :class:`QmdDaemonWedged` carrying the tail of the
daemon's log. The tail is the point. A timeout with no evidence attached
says only that the deadline fired, which is the one thing the caller
already knew.

Which failures are which is decided by :func:`classify_call_error`, and
that function has exactly one caller outside this module:
``QmdRecycleToolset``, the pydantic-ai wrapper on the agent path. The
classification is a transport-level fact, not an agent fact, so it lives
here; the wrapper re-wraps the answer in ``ModelRetry``/``ToolFailed``
because a model needs to be *told* a tool failed, and a library caller
needs an exception. ``ModelRetry`` must not leak past this module.
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

#: Everything the daemon cannot serve. Index maintenance (``update``,
#: ``embed``, ``cleanup``), the collection and daemon lifecycle verbs,
#: diagnostics (``ls``, ``doctor``, ``bench``), and — the one that is a
#: capability gap rather than a placement choice — BM25 ``search``.
CLI_ONLY_OPS: frozenset[str] = frozenset(
    {
        "search",
        "update",
        "embed",
        "cleanup",
        "ls",
        "doctor",
        "bench",
        "collection",
        "mcp",
    }
)

# Probe deadline for "is anything listening". Deliberately not the
# retrieval budget: a slow answer to "is this alive?" is itself the
# failure, and the seam has a real call to make once the answer arrives.
_PROBE_TIMEOUT_S = 0.5

# Tail of the daemon's own log attached to a wedge. Enough to show which
# phase it reached; the log is truncated on every daemon start, so this
# never grows without bound.
_LOG_TAIL_BYTES = 2000


class QmdDaemonUnavailable(RuntimeError):
    """The qmd daemon is not serving.

    Raised when the TCP probe fails, and when a call still cannot be
    made after a recycle. The message names both the URL the seam
    probed and the command that starts the daemon, because the operator
    reading it is the only one who can fix it.
    """


class QmdDaemonWedged(RuntimeError):
    """The qmd daemon accepted the call and stopped answering.

    Carries ``last_output`` — the tail of qmd's own log at the moment of
    the wedge. Without it the exception says only that the read deadline
    fired, which tells the reader nothing they did not already know.

    The tail must be captured *before* the recycle. qmd truncates
    ``mcp.log`` on every daemon start, so a tail read afterwards is the
    replacement daemon's log, not the one that wedged.
    """

    def __init__(self, message: str, *, last_output: str = "") -> None:
        super().__init__(message)
        self.last_output = last_output


# --- the cached client -------------------------------------------------
#
# Cached per process so the daemon's model is not re-warmed per call
# (3.11s warm against 10.77s cold). What is cached is the *client
# object*, not a connection: an MCP session is bound to the event loop
# that opened it, and this seam is called from `asyncio.run` bridges that
# each get a fresh loop. A cached session would outlive its loop; a
# cached client rebuilt from a dead one is merely a small allocation.
#
# Cache key is the daemon URL. The cached client is rebuilt on a
# ``LIES_QMD_URL`` change (``_reset_client`` is exported for tests
# and operator tooling); the read deadline is *not* part of the key
# — a deadline change is read on the next call via the per-call
# ``timeout=`` argument to :func:`daemon_tool`, which forwards to
# ``fastmcp.Client.call_tool`` and overrides the client-level
# deadline for that call only.

_client: fastmcp.Client | None = None
_client_url: str | None = None


def _daemon_client(url: str) -> fastmcp.Client:
    """The process-wide qmd client for ``url``, built on first use."""
    global _client, _client_url
    if _client is not None and _client_url == url:
        return _client
    # Imported here, not at module scope: `lies.qmd.mcp` imports
    # `classify_call_error` from this module, so a top-level import here
    # would close a cycle that only resolves halfway. The factory is
    # needed exactly once per process, on the path that is about to do
    # I/O anyway.
    from lies.qmd.mcp import _build_qmd_httpx_client

    _client = fastmcp.Client(
        StreamableHttpTransport(
            url,
            # The same factory the agent path's toolset uses, so both
            # paths reach the daemon with the same deadlines and the
            # same client shape.
            httpx_client_factory=_build_qmd_httpx_client,
        )
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
# What actually arrives at this boundary is not what the fastmcp docs
# suggest, and the difference decides whether any of it works. Measured
# against the installed fastmcp 4.0.3 (see the probe in the module
# docstring of tests/unit/qmd/test_access.py):
#
#   daemon not listening        RuntimeError("Client failed to connect:
#                                 All connection attempts failed")
#                                 __cause__ = httpx2.ConnectError
#   connect ok, call wedges     httpx2.ReadTimeout
#   connect ok, session dies    mcp.MCPError(-32000, "Connection closed")
#
# Two things follow, and both were live defects before this module:
#
# 1. **fastmcp 4 vendors its own httpx as `httpx2`.** `httpx2.ReadTimeout`
#    and `httpx.ReadTimeout` are unrelated classes, so a taxonomy written
#    against `httpx` matches nothing the client actually raises. Both
#    generations are checked below, and the check is by class *name*
#    rather than by importing `httpx2`, so this module does not take a
#    dependency on fastmcp's vendoring choice.
#
# 2. **fastmcp wraps a dead session in a bare `RuntimeError`**, with the
#    real transport error on `__cause__`. Matching the wrapper's message
#    is matching a string another package owns; `_transport_cause`
#    follows the cause chain instead, which is what the wrapper
#    documents it is for.

#: Exception class names that mean "the daemon accepted the request and
#: then stopped answering", across httpx generations. Matched by name
#: because the two httpx packages are unrelated types.
_WEDGE_NAMES = frozenset({"ReadTimeout", "WriteTimeout", "PoolTimeout"})

#: Exception class names that mean "the daemon was not there".
_TRANSPORT_NAMES = frozenset({"TransportError", "TimeoutException", "ConnectError"})

#: JSON-RPC code the MCP SDK raises when the session dies under an
#: in-flight call (``mcp.types.CONNECTION_CLOSED``). Spelled as a literal
#: because the constant moved between SDK releases and a failed import
#: here would take the whole seam down over a version detail.
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


def _transport_cause(exc: BaseException) -> Exception | None:
    """The transport error underneath fastmcp's dead-session wrapper.

    fastmcp presents a dead session as ``RuntimeError("Client failed to
    connect: ...")`` from ``_build_session_error``, keeping the original
    on ``__cause__``. Follow the chain and return the first exception
    that is itself a transport failure, or ``None`` if there is none.

    A ``BaseException`` on the chain that is not an ``Exception`` (a
    ``CancelledError``, a ``KeyboardInterrupt``) ends the walk rather
    than being returned: it is not a transport failure, and
    re-classifying it would be a lie.
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

    ``action`` is one of:

    - ``"recycle-raise"`` — the daemon is wedged. Restart it, then raise:
      a fresh daemon re-wedges on the same payload, so retrying the same
      request is a slower way to reach the same answer.
    - ``"recycle-retry"`` — the daemon was unreachable or mid-restart.
      Restart it and try once more; the retry failing is terminal.
    - ``"passthrough"`` — not a transport failure at all (a protocol
      rejection such as ``-32602 Invalid params``). The caller re-raises
      it unchanged; recycling cannot change the daemon's answer.

    ``retryable`` is ``True`` only for ``"recycle-retry"``, so a caller
    that wants just the yes/no still reads correctly.

    Order matters: a read timeout is *also* a transport error, so the
    wedge case is decided first or every wedge would be silently
    retried against a daemon that re-wedges on the same payload. A
    connect timeout is a transport error and not a wedge — that is a
    daemon that was not there yet, not one that stopped answering.

    Deliberate gap: ``httpx.HTTPStatusError`` (a 5xx from a daemon
    failing internally) and ``httpx.RemoteProtocolError`` (a read that
    died on a broken connection) classify as ``"passthrough"`` and get
    no recycle. Both are server-side states rather than reachability
    states, the design names exactly three failure modes, and a restart
    is a heavier answer than either warrants. Recorded so a later
    reader knows the boundary was considered rather than missed.
    """
    if _is_wedge(exc):
        return ("recycle-raise", False)
    if _is_transport(exc):
        return ("recycle-retry", True)
    cause = _transport_cause(exc)
    if cause is not None:
        return classify_call_error(cause)
    if isinstance(exc, mcp.MCPError):
        if exc.code == _CONNECTION_CLOSED:
            # The session died while the call was in flight. Same
            # situation as a read timeout, reported through the protocol
            # layer because the dispatcher saw the socket go first.
            return ("recycle-raise", False)
        if exc.code == httpx.codes.REQUEST_TIMEOUT:
            return ("recycle-retry", True)
    return ("passthrough", False)


# --- the seam ----------------------------------------------------------


async def daemon_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    timeout: float | None = None,
) -> Any:
    """Call one qmd daemon tool, recovering per the taxonomy above.

    ``timeout``, when set, is the per-call read deadline in seconds —
    forwarded to ``fastmcp.Client.call_tool`` as its ``timeout=``
    keyword, which threads through to the per-request MCP read
    timeout (``read_timeout_seconds`` in
    ``fastmcp/client/mixins/tools.py``). ``None`` falls back to the
    client-level deadline baked into the cached httpx client at
    factory construction (``mcp._default_read_timeout_s()``); a
    explicit ``timeout`` wins over it. The seam and the CLI's
    ``_run_qmd`` both honour the same env var
    (``LIES_QMD_FANOUT_TIMEOUT``) and read it at call time — a value
    the CLI path would have applied is now applied here too, so a
    deadline change takes effect on the next call rather than waiting
    for the cached client to be invalidated.

    Returns the raw ``CallToolResult``. Callers read ``.content`` —
    ``get``/``multi_get`` answer with a content block and ``.data`` is
    ``None``, so a caller reaching for ``.data`` stores an empty body.

    Raises:
        ValueError: ``name`` is not a daemon tool. The capability map is
            checked here rather than trusted to every call site, because
            a CLI-only operation sent to the daemon comes back empty and
            empty reads downstream as "the corpus has nothing".
        QmdDaemonUnavailable: the daemon is not serving.
        QmdDaemonWedged: the daemon stopped answering.
        RuntimeError: the daemon returned a tool error; the message
            carries its text.
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
        # Stale is not an error: the daemon is up and serving an index
        # that predates the last write. Reap and respawn so the call
        # reads what was just written, then make the call as planned.
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
            # Which daemon wedged? The first call, on the one running
            # when this except was entered. Name it D1 — the recycle
            # immediately below spawns its replacement, D2, and qmd
            # truncates mcp.log on every start, so the tail has to be
            # read before that recycle or it describes D2 instead. A
            # log attributed to the wrong daemon is worse than no log:
            # it is confidently wrong, and in the one direction this
            # field exists to prevent.
            #
            # Branch-local rule, not a project-wide one. The retry path
            # below reads the tail *after* its recycle, and that is
            # correct there because the wedge it reports happened in the
            # daemon that recycle started. "Read before the recycle"
            # holds on this branch because D1 is the daemon that wedged;
            # the question is always *which daemon wedged*, never
            # *which side of the recycle am I on*.
            #
            # Read on this branch only. A transport error recycles and
            # retries without ever carrying a tail, so reading on that
            # path would spend an open+seek+read of the daemon's log on
            # a value that is thrown away — on the common path, where a
            # daemon that is restarting is ordinary rather than
            # exceptional.
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
            # The retry's failure is classified, not assumed. Three
            # outcomes, and collapsing them would repeat the mistake
            # this seam exists to fix — telling the operator something
            # about the daemon that is not true:
            #
            #   passthrough  — a reason we do not own (a malformed
            #                 result, a decode error). Propagates; it
            #                 says nothing about reachability.
            #   recycle-raise — the fresh daemon wedged too. That is a
            #                 wedge, not a down daemon, and the log tail
            #                 is still the useful evidence.
            #   recycle-retry — still unreachable. The only case that is
            #                 genuinely "start the daemon".
            retry_action = classify_call_error(retry_exc)[0]
            if retry_action == "passthrough":
                raise
            if retry_action == "recycle-raise":
                # Which daemon wedged? The retry's — call it D2, the one
                # `await _recycle(url)` above spawned. The recycle
                # truncated mcp.log and D2 has been writing to it since,
                # so reading *here* is what puts D2's own last words on
                # the exception.
                #
                # This is the mirror image of the first branch, where
                # the read precedes the recycle because the wedge
                # happened in the daemon that existed beforehand. The
                # rule is not "read before" or "read after" — it is
                # "read from the daemon that wedged", and the side of
                # the recycle is only how you tell which one that is.
                # Moving this read earlier, to "match" the branch above,
                # would attach D1's pre-recycle log to a wedge that
                # happened in D2.
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

    ``raise_on_error=False`` so a tool error arrives as a result to read
    rather than an exception to unwrap; the text qmd wrote is more
    useful than any phrasing this module could add.

    ``timeout``, when set, is the per-call read deadline in seconds
    forwarded to ``client.call_tool``. ``None`` defers to the
    client-level deadline built into the cached httpx client at
    factory construction (see :func:`_daemon_client`).
    """
    async with client:
        # ``timeout=None`` is intentionally NOT forwarded: the cached
        # client's read deadline is the one that applies, and a wire
        # capture of ``timeout=None`` would lie to a reader about
        # whether the seam intends to override. ``None`` means "do
        # nothing extra" rather than "send a None".
        if timeout is None:
            result = await client.call_tool(
                name,
                arguments,
                raise_on_error=False,
            )
        else:
            result = await client.call_tool(
                name,
                arguments,
                raise_on_error=False,
                timeout=timeout,
            )
    if getattr(result, "is_error", False):
        raise RuntimeError(f"qmd tool {name!r} failed: {_result_text(result)}")
    return result


def _result_text(result: Any) -> str:
    """The text blocks of a call result, joined."""
    blocks = getattr(result, "content", None) or []
    return " ".join(getattr(block, "text", "") for block in blocks).strip()


def _recycle_data_dir() -> Path:
    """The ``data-dir`` to *record* for a daemon started by :func:`_recycle`.

    Read from the sidecar — what the running daemon was recorded with —
    falling back to the library root, the index every library-mode
    collection is registered under.

    **Decided, and the reasoning matters if this is ever changed.**
    ``ensure_qmd_daemon`` takes a ``data_dir`` that looks like it
    controls the daemon, and it does not: ``_spawn_qmd_daemon`` runs
    ``qmd mcp --http --daemon`` with ``cwd=Path.cwd()`` and never reads
    the argument (``daemon.py:252-261``). ``data_dir`` only ever reaches
    ``write_sidecar_data_dir``. So a recycle and an ensure can *disagree*
    on what to record — ``ensure`` is handed ``wiki.wiki_dir`` by
    ``operator.py:151``, this falls back to ``library_git_root()`` — and
    **the daemon that comes up is the same either way**, because neither
    value is passed to qmd.

    The blast radius of that disagreement is therefore bounded and small:
    the sidecar records one path or the other, and the consequence is
    only that a *later* ``ensure_qmd_daemon`` with the other value sees
    ``check_data_dir_match`` as False and reaps a healthy daemon once.
    It is not a wrong-index read: the daemon serves whatever qmd's global
    index holds, and neither value changes that. The user-visible cost is
    one unnecessary restart.

    Left as-is deliberately. Making the two agree means changing which
    surface owns the daemon's recorded identity — a design decision
    about ``ensure_qmd_daemon``'s signature, wider than a routing branch
    should carry, and with a real behavioural consequence either way
    (reaping more, or less). Recorded here so the next reader meets the
    reasoning at the code rather than having to rediscover that the
    argument looks load-bearing and is not.
    """
    recorded = read_sidecar_data_dir()
    if recorded is not None:
        return recorded
    from lies.library.registry import library_git_root

    return library_git_root()


async def _recycle(url: str) -> None:
    """Restart the daemon. A recycle that never served is logged, not raised.

    A failed restart does not replace the caller's diagnosis. The wedge
    is the evidence; a recycle that could not serve afterwards is a
    second fact about it, and raising *that* instead would tell the
    reader the wrong thing about what happened to their request.

    Only ``QmdRecycleFailed`` — reap+spawn+probe exhausted its budget —
    is absorbed. A genuine I/O error writing the sidecar still
    propagates, because that one is not about qmd at all.
    """
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
    # `lifecycle._logfile` is qmd's log path, the same one the operator
    # reads after `lies qmd up`. Reused rather than re-spelled: a second
    # copy of this path is a second answer to "where is qmd's log".
    from lies.qmd.lifecycle import _logfile

    try:
        with _logfile().open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", errors="replace").strip()
    except OSError:
        # No log, or unreadable. The wedge is still worth reporting; it
        # just arrives without the tail.
        return ""
