"""Ensure and inspect qmd's own MCP daemon.

qmd owns its lifecycle: ``qmd mcp --http --daemon`` is idempotent (a
second call prints ``Already running (PID N)`` and exits 0), and
``qmd status`` reports the running pid. LIES therefore keeps no record
of qmd's pid — a second copy of that truth could only drift.

The daemon is machine-global: one fixed port, one index under
``~/.cache/qmd``. Several wikis and unrelated tools share it. That is
why this module exposes no public stop function — operators who want
to stop it run ``qmd mcp stop`` themselves. Killing it would break
sessions LIES knows nothing about, exactly like killing a host-spawned
stdio server.

The one exception is :func:`_reap_qmd_daemon`: an internal helper that
SIGTERMs the daemon when the sidecar records a different ``data-dir``
than the wiki we are about to serve. Without it, the newly-spawned
qmd would race the old daemon for the port and could inherit its
index. It is the only path where LIES reaps a daemon it does not own.

Every function here is non-fatal. A wiki server that refused to start
because its search backend was down would be a worse failure than
degraded search, so failures are reported through :class:`QmdState`
rather than raised.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path as _Path

import httpx

import fastmcp
import mcp

from lies.qmd._subprocess import _run_qmd

_log = logging.getLogger(__name__)

_QMD_BIN = "qmd"
_MCP_LINE = re.compile(r"^MCP:\s+running\s+\(PID\s+(\d+)\)", re.MULTILINE)
_ALREADY_RUNNING = re.compile(r"Already running\s+\(PID\s+(\d+)\)")

SIDECAR_PATH = _Path(
    os.environ.get("LIES_QMD_SIDECAR_OVERRIDE")
    or (_Path.home() / ".local" / "share" / "qmd" / "mcp.data-dir")
)


def read_sidecar_scope() -> _Path | None:
    """Read the LIES *scope* recorded for the current qmd daemon session.

    Returns ``None`` when the sidecar is absent (first-run case).

    The name says scope, not data-dir, because that is what it holds.
    See :func:`_canonical_scope` for why the value has no meaning to qmd.
    """
    if not SIDECAR_PATH.exists():
        return None
    return _Path(SIDECAR_PATH.read_text().strip())


def write_sidecar_scope(scope: _Path) -> None:
    """Record the LIES scope for the current qmd daemon session.

    Called from :func:`ensure_qmd_daemon` after a successful start.
    """
    SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIDECAR_PATH.write_text(str(scope))


def check_scope_match(expected: _Path) -> bool:
    """Return True if the sidecar's scope matches ``expected`` (or is absent).

    Paths are normalized via :meth:`Path.resolve` before comparison so
    callers that pass ``Path("wiki")`` vs ``Path("./wiki")`` do not get
    a spurious mismatch from a leading-dot or relative-segment drift.
    """
    actual = read_sidecar_scope()
    if actual is None:
        return True
    return actual.resolve() == expected.resolve()


def _canonical_scope() -> _Path:
    """The one scope value every caller passes.

    **What the value is for.** It answers exactly one question — *"is the
    daemon that is serving the one LIES started?"* — and nothing else. It
    never selects an index, never reaches a spawn, and is never opened;
    it is only ever *compared* (by :func:`check_scope_match`) and
    *recorded*. A value that is constant across every wiki is therefore
    correct for that question: on a wiki-only install
    ``library_git_root()`` resolves to a path that need not exist, and
    that is fine, because nothing ever reads it.

    What it cannot do is distinguish two concurrent LIES sessions. That
    is inherent to a machine-global daemon: there is one qmd daemon
    per host, so there is one scope per host, and no value in a
    sidecar can do better without the daemon learning a concept it does
    not have.

    ``library_git_root()``. It used to be three different values from
    three call sites — ``wiki.wiki_dir`` (``operator.py``'s ``up``),
    ``wiki.data_root`` (``operator.py``'s ``recycle``), and
    sidecar-or-``library_git_root()`` (``access._recycle_scope``) — and
    the system coped only because the value never reached the spawn, so
    :func:`check_scope_match` was comparing three unrelated directories
    and could never match across call sites. ``wiki.wiki_dir`` is
    ``data_root / "wiki"``, so the first two differ *by construction*
    and no amount of agreement makes them so.

    One canonical value is what makes the comparison mean something.
    """
    from lies.library.registry import library_git_root

    return library_git_root()


def daemon_port_from_url(url: str) -> int | None:
    """The explicit port in ``url``, or ``None`` when it carries none.

    ``http://127.0.0.1:8181/mcp`` -> ``8181``. A bare origin, or a
    scheme-less string with no ``:``, -> ``None``, which callers read as
    "let qmd choose its own default" rather than as port 0.

    A daemon restarted onto the wrong port is the failure this exists
    to prevent: the reap kills the daemon on the configured port, a
    spawn that omits ``--port`` brings up a *stray* on 8181 and takes
    its pidfile, and the readiness probe then polls the configured port
    for the whole budget while the operator's daemon is already gone.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url if "//" in url else f"//{url}")
    try:
        return parts.port
    except ValueError:  # a non-numeric port is not a port
        return None


def _daemon_start_time() -> float | None:
    """mtime of qmd's mcp.pid (proxy for daemon launch time)."""
    raw = os.environ.get("XDG_CACHE_HOME", "")
    cache = _Path(raw) if raw and not raw.startswith("${") else _Path.home() / ".cache"
    pid_path = cache / "qmd" / "mcp.pid"
    try:
        return pid_path.stat().st_mtime
    except OSError:
        return None


def _global_last_write_marker() -> _Path:
    """Single machine-global sentinel, sibling of qmd's mcp.pid and mcp.data-dir.

    The qmd daemon is machine-global and indexes whatever libraries +
    wikis the operator mounts. A per-data-dir marker would miss the
    case where the daemon serves wiki A but wiki B (or any library)
    was written since the daemon started. One marker, touched by every
    write envelope (``LibraryWriter.commit`` +
    ``WikiMemoryService.apply_plan``), reflects the union of "anything
    has been written."
    """
    raw = os.environ.get("XDG_CACHE_HOME", "")
    cache = _Path(raw) if raw and not raw.startswith("${") else _Path.home() / ".cache"
    return cache / "qmd" / "last-write-marker"


def _is_daemon_stale() -> bool:
    """True when any wiki or library has been written since the daemon started."""
    marker = _global_last_write_marker()
    daemon_start = _daemon_start_time()
    if daemon_start is None or not marker.exists():
        return False
    return marker.stat().st_mtime > daemon_start


@dataclass(frozen=True)
class QmdState:
    """What LIES knows about qmd right now.

    ``detail`` is a human-readable line for ``lies mcp status`` and for
    the single stderr warning ``up`` prints when qmd is unavailable.
    """

    installed: bool
    running: bool
    pid: int | None
    detail: str


def qmd_installed() -> bool:
    """Return True if the ``qmd`` binary is on PATH."""
    return shutil.which(_QMD_BIN) is not None


def _not_installed() -> QmdState:
    return QmdState(
        installed=False,
        running=False,
        pid=None,
        detail="qmd is not installed (not on PATH); search runs degraded",
    )


# See ``lies.qmd.lifecycle`` for why this is not the retrieval budget:
# a slow answer to "is this alive?" is itself the failure, so a wedged
# daemon is reported rather than waited on.
STATUS_TIMEOUT_S = 15.0


def qmd_daemon_state() -> QmdState:
    """Report qmd's daemon state by parsing ``qmd status``.

    Never raises: any failure is reported as ``running=False`` with the
    reason in ``detail``.
    """
    if not qmd_installed():
        return _not_installed()
    try:
        proc = _run_qmd(
            [_QMD_BIN, "status"],
            cwd=_Path.cwd(),
            timeout=STATUS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return QmdState(True, False, None, "qmd status timed out")
    except OSError as exc:
        return QmdState(True, False, None, f"qmd status failed: {exc}")

    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        stderr = (stderr or "").strip()
        first = stderr.splitlines()[0] if stderr else "no stderr"
        return QmdState(True, False, None, f"qmd status exited {proc.returncode}: {first}")
    match = _MCP_LINE.search(stdout or "")
    if match is None:
        return QmdState(True, False, None, "qmd daemon not running")
    pid = int(match.group(1))
    return QmdState(True, True, pid, f"qmd daemon running (pid {pid})")


def _pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is alive (and not a zombie). Never raises.

    Zombies respond to ``os.kill(pid, 0)`` with success — the PID still
    exists in the process table until the parent reaps it — but they
    have released every resource we care about (file descriptors,
    sockets, ports). Distinguishing them is what lets the reap helper
    return promptly for a daemon that exited on SIGTERM instead of
    waiting the full grace for an already-dead process.
    """
    status_path = f"/proc/{pid}/status"
    try:
        with open(status_path) as f:
            for line in f:
                if line.startswith("State:"):
                    state = line.split(":", 1)[1].strip()
                    return not state.startswith(("Z", "X"))
    except OSError:
        return False
    return False


def _reap_qmd_daemon(*, grace: float = 2.0, poll: float = 0.05) -> None:
    """Kill the running qmd daemon process and wait for it to exit.

    SIGTERM first; if the process is still alive past ``grace`` seconds,
    escalate to SIGKILL. ``poll`` is the wait-loop interval. Never
    raises. Without the wait, the subsequent :func:`_spawn_qmd_daemon`
    could race the dying daemon for the port and inherit its index,
    while the sidecar already recorded the new ``data-dir``.
    """
    state = qmd_daemon_state()
    if not state.running or state.pid is None:
        return
    pid = state.pid

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(poll)

    # Past grace — escalate.
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(poll)


def _spawn_qmd_daemon(
    *,
    port: int | None = None,
    data_dir: "_Path | None" = None,
    timeout: float | None = None,
    raise_on_error: bool = False,
) -> "subprocess.CompletedProcess[bytes] | None":
    """Invoke ``qmd mcp --http --daemon``. The ONLY place qmd is spawned.

    Args:
        port: optional ``--port``. qmd honours it (verified in qmd
            2.5.3, ``dist/cli/qmd.js:4005``:
            ``const port = Number(cli.values.port) || 8181``). ``None``
            omits the flag and lets qmd apply its own default.
        data_dir: optional qmd cache root, delivered as
            ``XDG_CACHE_HOME``. **There is no ``--data-dir`` flag** —
            the ``case "mcp":`` block reads only ``cli.args[0]``,
            ``values.http``, ``values.port``, ``values.daemon`` and
            ``values.index``. qmd resolves its whole cache dir from
            ``XDG_CACHE_HOME`` (``dist/cli/qmd.js:3981-3983`` and
            ``dist/store.js:428-434``), covering
            ``index.sqlite``, ``mcp.pid`` and ``mcp.log``. Note the
            off-by-one: qmd appends its own ``qmd/``, so a daemon
            started with ``data_dir=X`` uses ``X/qmd/``, not ``X``.

            This is the ONLY argument here that selects an index, and
            it is deliberately **not** wired to the ``scope`` the
            sidecar records — see :func:`_canonical_scope` for why
            those are different questions. Production passes neither.
        timeout: seconds before SIGKILL. Defaults to
            ``STATUS_TIMEOUT_S``.
        raise_on_error: propagate a spawn failure instead of swallowing
            it. Only :func:`ensure_qmd_daemon`'s normal path sets it,
            because that path reports the concrete reason
            ("timed out after Ns" / "failed: ...") to the operator; every
            other caller wants the swallow.

    Returns:
        The ``CompletedProcess`` on success — :func:`ensure_qmd_daemon`
        reads qmd's ``Already running (PID N)`` line off stdout, so a
        ``None`` return would lose that. ``None`` when the spawn failed
        and ``raise_on_error`` is false.

    **Why ``port`` is not refused without ``data_dir``.** An earlier
    version raised here, on the reasoning that a second daemon sharing a
    cache root would take over ``mcp.pid``. The hazard is real but the
    guard was wrong: the legitimate production use is *restart THE
    daemon on the configured port*, which necessarily shares the real
    cache root, and refusing it broke exactly that. Concretely, against
    ``LIES_QMD_URL=http://127.0.0.1:9000/mcp`` the reap kills the 9000
    daemon and a port-less spawn brings up a **stray on 8181** which
    overwrites ``mcp.pid``; the probe then polls 9000 for the whole
    budget and the operator gets ``QmdRecycleFailed`` having lost their
    daemon and gained a squatting one.

    The hazard is real but it is not this function's to refuse — it is
    the caller's precondition. Both production callers satisfy it:
    :func:`recycle_qmd_daemon` and :func:`ensure_qmd_daemon` both
    :func:`_reap_qmd_daemon` first, so no live daemon holds the pidfile
    when the spawn runs, which is the precondition qmd itself checks
    (``dist/cli/qmd.js:4008-4013`` refuses to start while a live pidfile
    exists). Passing a port with a *different* cache root than the one
    the reap cleared breaks that, and is what the throwaway-spawn tests
    use ``data_dir`` for.
    """
    argv = [_QMD_BIN, "mcp", "--http", "--daemon"]
    env_overrides: dict[str, str] = {}
    if port is not None:
        argv += ["--port", str(port)]
    if data_dir is not None:
        env_overrides["XDG_CACHE_HOME"] = str(data_dir)
    try:
        return _run_qmd(
            argv,
            cwd=_Path.cwd(),
            timeout=STATUS_TIMEOUT_S if timeout is None else timeout,
            env_overrides=env_overrides,
        )
    except subprocess.TimeoutExpired, OSError:
        if raise_on_error:
            raise
        return None


def ensure_qmd_daemon(
    *,
    scope: _Path,
    port: int | None = None,
) -> QmdState:
    """Start qmd's http daemon if not already up; reap and respawn if the
    sidecar records a different scope OR the daemon is serving a
    stale index (F14: marker mtime > daemon pidfile mtime).

    Args:
        scope: the LIES scope to record in the sidecar. **This does not
            select the daemon's index** and never reaches the spawn — see
            :func:`_canonical_scope`. Pass ``_canonical_scope()``.
        port: passed through to :func:`_spawn_qmd_daemon` so the daemon
            comes up where ``LIES_QMD_URL`` says it is. ``None`` omits
            ``--port`` and lets qmd default.

    All three paths below go through :func:`_spawn_qmd_daemon`. That used
    to be two of them: the "normal path" inlined its own
    ``[_QMD_BIN, "mcp", "--http", "--daemon"]`` with its own ``timeout``,
    so a pin that counted ``_spawn_qmd_daemon()`` call sites could not see
    it — and a ``--port`` added to that copy would have changed where the
    daemon binds without turning any test red.
    """
    if not qmd_installed():
        return _not_installed()

    if not check_scope_match(scope):
        # Foreign daemon serving the wrong index; reap and respawn.
        _reap_qmd_daemon()
        _spawn_qmd_daemon(port=port)
        write_sidecar_scope(scope)
        return qmd_daemon_state()

    if _is_daemon_stale():
        # F14: the wiki has been written since the daemon started.
        # Reap+respawn via the sync path (no probe poll — ensure_qmd_daemon
        # is sync and called from sync CLI + LibraryWriter envelopes; making
        # it async would ripple into every caller).
        _log.info("ensure_qmd_daemon: daemon stale by marker; reaping via F14")
        _reap_qmd_daemon()
        _spawn_qmd_daemon(port=port)
        write_sidecar_scope(scope)
        return qmd_daemon_state()

    # Normal path: idempotent start. ``raise_on_error`` because the two
    # QmdStates below name the concrete reason to the operator, and a
    # swallowed spawn would collapse both into "starting qmd failed".
    try:
        proc = _spawn_qmd_daemon(port=port, raise_on_error=True)
    except subprocess.TimeoutExpired:
        return QmdState(True, False, None, f"starting qmd timed out after {STATUS_TIMEOUT_S:g}s")
    except OSError as exc:
        return QmdState(True, False, None, f"starting qmd failed: {exc}")
    assert proc is not None  # raise_on_error=True means a return implies success
    write_sidecar_scope(scope)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    output = f"{stdout or ''}{stderr or ''}"
    already = _ALREADY_RUNNING.search(output)
    if already is not None:
        pid = int(already.group(1))
        return QmdState(True, True, pid, f"qmd daemon already running (pid {pid})")
    if proc.returncode != 0:
        first = output.strip().splitlines()[0] if output.strip() else "no output"
        return QmdState(True, False, None, f"qmd exited {proc.returncode}: {first}")
    return qmd_daemon_state()


# --- Added for the qmd-daemon-recycle spec (2026-09-14) ---


class QmdRecycleFailed(RuntimeError):
    """qmd daemon recycled but never served within ready_timeout.

    Carries the last observed ``QmdState`` so callers can branch on the
    concrete failure reason (no listener, exit code, timeout) without
    parsing the message string.
    """

    def __init__(self, ready_timeout_s: float, last_state: "QmdState") -> None:
        super().__init__(
            f"qmd daemon recycled but never served within "
            f"{ready_timeout_s:g}s (last state: {last_state.detail})"
        )
        self.ready_timeout_s = ready_timeout_s
        self.last_state = last_state


#: Exceptions the readiness probe treats as "not serving yet", so the
#: poll loop keeps going. Measured against a stopped qmd daemon on
#: 2026-10-10 (fastmcp 4.0.10), not guessed:
#:
#: - **Connection refused** (a daemon that is down — the case this loop
#:   exists for) arrives as a **bare** ``RuntimeError("Client failed to
#:   connect: All connection attempts failed")``, with the real error on
#:   ``__cause__`` as ``httpx2.ConnectError``. That is fastmcp's own
#:   doing: ``fastmcp/client/client.py:1048`` calls
#:   ``_connection_failure``, whose docstring says it presents a dead
#:   session identically "wherever it is noticed" and wraps everything
#:   that is not ``httpx2.HTTPStatusError | MCPError`` into a
#:   ``RuntimeError``. ``RuntimeError`` matches none of the three arms
#:   below.
#: - **HTTP-level not-ready** (a booting daemon answering 404/503)
#:   arrives as ``mcp.shared.exceptions.MCPError`` — measured for both,
#:   raised from ``mcp/shared/jsonrpc_dispatcher.py:461``.
#:
#: The ``httpx.HTTPError`` arm is kept for LIES' own transport calls.
#: It does **not** cover the probe's failure: fastmcp vendors its own
#: httpx as ``httpx2`` (2.13.1, a distinct module from LIES' 0.28.1),
#: so ``httpx2.ConnectError`` is not an ``httpx.HTTPError``. It is not
#: an ``OSError`` either. Only the wrapping ``RuntimeError`` is
#: catchable without importing a vendored module name.
#:
#: Before this tuple was widened the loop raised out of the function on
#: the **first** probe against a stopped daemon: it never retried,
#: never spent ``ready_timeout``, and never reached its own
#: ``QmdRecycleFailed`` contract. See
#: ``tests/unit/qmd/test_daemon_recycle.py``.
#:
#: **Why ``RuntimeError`` is safe here, given how broad it is.** fastmcp
#: constructs a bare ``builtins.RuntimeError`` (``client.py:248``) with no
#: subclass to catch, so no narrower guard exists; matching
#: ``str(exc).startswith("Client failed to connect:")`` would bind LIES to a
#: third-party message string, and ``import httpx2`` is exactly the
#: vendored-module import the taxonomy bullet warns against. Two facts make
#: the breadth safe, and both are load-bearing: ``QmdRecycleFailed`` **is**
#: a ``RuntimeError`` (it subclasses it), and nothing inside the ``try``
#: raises it — the ``raise`` is at the bottom of the function, outside the
#: guarded block. If a ``raise QmdRecycleFailed`` ever appeared inside that
#: ``try``, this arm would swallow LIES' own failure contract and the loop
#: would spin to the budget instead of reporting. The pin below covers the
#: **bare** form ``raise QmdRecycleFailed(...)``: it does not see a raise
#: reached through an attribute (``daemon.QmdRecycleFailed(...)``), via a
#: pre-built instance (``boom = ...; raise boom``), or through a helper
#: called inside the try. It claims exactly what it enforces, no more.
#: ``tests/unit/qmd/test_daemon_recycle.py`` asserts the subclass relation
#: behaviourally and the try/except shape over the AST.
_RECYCLE_PROBE_ERRORS = (httpx.HTTPError, mcp.MCPError, OSError, RuntimeError)


async def recycle_qmd_daemon(
    *,
    scope: "_Path",
    daemon_url: str,
    ready_timeout: float = 30.0,
    poll_interval: float = 0.5,
) -> "QmdState":
    """Restart qmd's daemon; poll list_tools() until it serves or budget expires.

    Always reap first (idempotent when not running), then spawn. The
    probe is a ``fastmcp.Client.list_tools()`` call against
    ``daemon_url`` — proves the JSON-RPC session is alive (not whether
    qexpander is warm; warm-up latency is borne by the first real call).

    Args:
        scope: the LIES scope to record in the sidecar. It does **not**
            select the daemon's index and never reaches the spawn — see
            :func:`_canonical_scope`. The daemon's index comes from the
            operator's environment, which the reap-then-spawn inherits.
        daemon_url: the URL to probe, and the source of the ``--port``
            the spawn must use (:func:`daemon_port_from_url`). Restarting
            onto a *different* port than the one probed is the failure
            that shape prevents.

    **Worst case: a ~30 s floor and a ~63 s ceiling**, measured, not
    derived (measured 2026-10-10). Every constant is scaled by 1/100, a
    real exhaustion is driven through the genuine ``_reap_qmd_daemon``,
    ``_spawn_qmd_daemon``, probe loop and deadline, and the result is
    scaled back. The harness is committed — see
    ``tests/integration/test_qmd_recycle_ceiling.py``, which re-derives
    these and asserts their *structure* rather than their decimals.

    **Scaled:** ``STATUS_TIMEOUT_S``, ``ready_timeout``,
    ``poll_interval``, the reap's ``grace``/``poll``, **and**
    ``_subprocess._POLL_INTERVAL_S``. That last one is neither a timeout
    nor a sleep — it is the reader loop's ``sel.select()`` granularity —
    so it does not scale by itself, and leaving it alone lands as up to
    ~6 s of real time per simulated hanging ``qmd status`` call. An
    earlier version of this measurement omitted it and inflated every
    hanging-status row by more than the bound being quoted.

    The axis that decides these figures is **what ``qmd status``
    reports**, not how fast it answers. That one fact sets whether the
    reap spends its two grace windows, whether it kills anything, and
    what each of its four status calls costs:

    ===================================  ======================
    configuration                         measured (real scale)
    ===================================  ======================
    daemon down, everything prompt          **30.4 s**  (floor)
    status reports "running", SIGTERM missed **34.5 s**
    daemon down, **spawn hangs**            **46.9 s**
    **qmd status hangs**                    **49.5 s**
    status and spawn both hang              **63.4 s**  (ceiling)
    ===================================  ======================

    Three things a reader should not get wrong:

    - **The spawn has its own 15 s bound.** "Prompt status" does not mean
      prompt everything, and the spawn hanging is the biggest single
      contributor at +16 s — named by no single-figure summary.
    - **The grace windows are reachable in exactly one row.** They cost
      ~4 s, and only where ``qmd status`` reports *running* with a live
      pid that then ignores SIGTERM. A hanging status reports
      ``running=False``, so ``_reap_qmd_daemon`` returns at its first
      line and spends neither. That is why the earlier "~80 s if status
      hangs" was **unreachable** rather than merely loose.
    - **The hanging-status row is genuinely unstable** — measured 17.7 s
      and 19.7 s over the floor on consecutive runs — because it turns on
      whether one more poll fits inside the 30 s budget when a status
      call is 15 s wide. Its calls *displace* polling rather than adding
      to it, so the net is one or two bounds, not three.

    Two earlier answers were wrong in ways worth recording: a single
    "~45 s" summed mutually exclusive component bounds, and the "~80 s"
    it became was not an upper bound at all. A later "+up to 4 s"
    variant then quoted a row whose delta was +5.3 s and which belonged
    to the configuration that forgoes those windows — the same defect,
    reintroduced one paragraph down.

    The per-poll ``qmd status`` is worth hoisting out of the loop (a
    subprocess per iteration, ~60 over a 30 s budget) and would collapse
    this table. That is a pre-existing cost, deliberately not changed
    here.

    Measured 2026-10-10: against a stopped daemon the loop previously
    escaped on probe one in ~0.3 s, so none of this had ever been
    observed being reached. These are ceilings, not typicals — a daemon
    that boots probes green on the first or second attempt and returns
    in well under a second.

    Raises:
        QmdRecycleFailed: reap+spawn+probe never served within ready_timeout.
            This is the **only** exception this function raises; the
            probe's own failures are absorbed into the retry loop.
    """
    _log.debug("recycle_qmd_daemon: reaping")
    _reap_qmd_daemon()
    _log.debug("recycle_qmd_daemon: spawning")
    # The port comes from the URL being probed. Without it the spawn
    # binds qmd's default and the probe waits out the whole budget for a
    # daemon that is listening somewhere else.
    _spawn_qmd_daemon(port=daemon_port_from_url(daemon_url))
    _log.debug("recycle_qmd_daemon: writing sidecar for %s", scope)
    write_sidecar_scope(scope)

    deadline = asyncio.get_event_loop().time() + ready_timeout
    last_state = qmd_daemon_state()
    while asyncio.get_event_loop().time() < deadline:
        try:
            assert fastmcp is not None  # type: ignore[assertion-that-fails]
            async with fastmcp.Client(daemon_url) as client:  # type: ignore[attr-defined]
                await client.list_tools()
            _log.debug(
                "recycle_qmd_daemon: probe succeeded after %gs",
                ready_timeout - (deadline - asyncio.get_event_loop().time()),
            )
            return qmd_daemon_state()
        except _RECYCLE_PROBE_ERRORS as exc:
            _log.debug("recycle_qmd_daemon: probe failed (%s); retrying", exc)
            # A `qmd status` subprocess **per failed poll** — ~60 over a
            # 30 s budget. Pre-existing, and deliberately not changed
            # here, but it is the reason the docstring's ceiling is two
            # figures rather than one, and it is the obvious thing to
            # hoist if that arithmetic is ever revisited: the state is
            # only read for the final ``QmdRecycleFailed``.
            last_state = qmd_daemon_state()
            await asyncio.sleep(poll_interval)
    raise QmdRecycleFailed(ready_timeout, last_state)
