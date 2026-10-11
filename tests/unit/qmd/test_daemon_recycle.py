"""The recycle readiness probe: what it retries on, and what it costs.

Every test here is stub-only and sleeps zero real seconds — the
``asyncio.sleep`` between attempts is replaced by a counter-driven no-op
and ``ready_timeout`` is set in the milliseconds. Spawning a real qmd
daemon is not this file's job; see
``tests/integration/test_qmd_spawn_throwaway.py``.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lies.qmd import daemon as qmd_daemon

# --- the shape a stopped daemon actually produces -----------------------
#
# Measured 2026-10-10 against a stopped qmd daemon (fastmcp 4.0.10), not
# read off a docs page:
#
#     builtins.RuntimeError: Client failed to connect: All connection attempts failed
#       --via cause--> httpx2.ConnectError: All connection attempts failed
#         --via context--> ExceptionGroup  -->  CancelledError  -->  WouldBlock
#
# fastmcp wraps it: `client.py:1048` calls `_connection_failure`, whose
# docstring says it presents a dead session identically "wherever it is
# noticed" and wraps anything that is not `httpx2.HTTPStatusError |
# MCPError` into a bare RuntimeError. `httpx2` is fastmcp's *vendored*
# httpx (2.13.1) — a different module from the httpx LIES imports
# (0.28.1) — so neither `httpx.HTTPError` nor `OSError` matches the
# cause. Only the wrapping RuntimeError is catchable without importing a
# vendored module name.
#
# The pre-existing tests raised `httpx.ConnectError` — LIES' own httpx —
# which IS in the old tuple. They passed, and the defect lived on.


_HTTPX2_MISSING = (
    "fastmcp's vendored httpx is not importable as 'httpx2'. That is the "
    "signal to RE-DERIVE _RECYCLE_PROBE_ERRORS against the installed "
    "fastmcp: probe a closed port and record the new exception class. "
    "Skip, not fail — a re-vendored httpx is not this suite's defect."
)


def _measured_dead_session_error() -> RuntimeError:
    """The exact exception ``fastmcp.Client(url).list_tools()`` raises.

    Skips rather than failing if ``httpx2`` is unimportable. A bare
    ``import httpx2`` would instead raise ``ModuleNotFoundError`` and take
    the assertion that names ``httpx2`` down with it — so the one test
    whose job is to say "re-derive this" is the first to die, and dies
    saying the wrong thing.
    """
    httpx2 = pytest.importorskip("httpx2", reason=_HTTPX2_MISSING)

    try:
        raise httpx2.ConnectError("All connection attempts failed")
    except httpx2.ConnectError as cause:
        err = RuntimeError("Client failed to connect: All connection attempts failed")
        err.__cause__ = cause
        return err


class _DeadSessionClient:
    """A ``fastmcp.Client`` stand-in for a daemon that is not serving.

    Every construction raises the measured shape — that is what a closed
    port does — and records its own attempt so the test can prove the
    loop retried instead of escaping.

    The log is class-level because ``monkeypatch.setattr`` hands the
    *class* to every construction, so instances cannot be enumerated from
    outside. It is reset explicitly by each test rather than assumed
    empty, which is what keeps it correct under ``pytest-randomly``.
    """

    attempts: list[str] = []

    def __init__(self, url: str) -> None:
        self.url = url
        type(self).attempts.append(url)

    async def __aenter__(self) -> _DeadSessionClient:
        raise _measured_dead_session_error()

    async def __aexit__(self, *args: object) -> None:
        return None

    async def list_tools(self) -> list[object]:
        raise AssertionError("__aenter__ raises first; this must never run")


@pytest.fixture(autouse=True)
def _no_socket_to_loopback() -> Iterator[None]:
    """Fail this file's tests if any of them opens a loopback socket.

    Every test here must stub ``fastmcp.Client``. One did not, and it
    dialed whatever was listening on the hardcoded port — which on this
    host is the operator's **live qmd daemon**, so the test's result
    depended on their machine: it passed only while nothing happened to
    be listening, and failed with ``DID NOT RAISE`` the moment a daemon
    was up. That is the worst failure mode a unit test has — silent when
    green, and a false verdict when red.

    Intercepting ``socket.socket.connect`` is what catches it:
    ``httpx2`` (fastmcp's vendored httpx) resolves a loopback URL
    through the ordinary socket path, so no fastmcp-specific knowledge is
    needed. The guard is module-wide rather than per-test because the
    mistake is one any test in this file could repeat — a stub is easy
    to forget on the next test someone adds.

    Restored with an explicit ``delattr`` rather than ``monkeypatch``:
    ``socket.socket.connect`` is *inherited* from ``_socket.socket``, so
    a monkeypatch undo would leave a shadowing class attribute behind
    rather than restoring the absence.
    """
    attempts: list[object] = []
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> Any:  # type: ignore[no-untyped-def]
        try:
            host = str(address[0])  # type: ignore[index]
        except TypeError, IndexError:
            host = ""
        if host.startswith("127.") or host in ("::1", "localhost"):
            attempts.append(address)
        return real_connect(self, address)  # type: ignore[arg-type]

    socket.socket.connect = guarded  # type: ignore[method-assign]
    try:
        yield
    finally:
        del socket.socket.connect
    assert not attempts, (
        f"this test opened {len(attempts)} loopback socket connection(s) "
        f"({attempts[:3]}). Unit tests must stub fastmcp.Client — one that "
        f"dials a real address silently becomes a function of whether the "
        f"operator happens to have a qmd daemon running."
    )


def _stub_spawn_and_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence everything in the recycle except its probe loop."""
    monkeypatch.setattr(qmd_daemon, "_reap_qmd_daemon", lambda **kw: None)
    monkeypatch.setattr(qmd_daemon, "_spawn_qmd_daemon", lambda **kw: None)
    monkeypatch.setattr(qmd_daemon, "write_sidecar_scope", lambda data_dir: None)
    monkeypatch.setattr(
        qmd_daemon,
        "qmd_daemon_state",
        lambda: qmd_daemon.QmdState(True, False, None, "no listener"),
    )


def _fake_clock(monkeypatch: pytest.MonkeyPatch, *, tick_s: float) -> dict[str, int]:
    """Give ``recycle_qmd_daemon`` a clock it drives, at zero wall cost.

    The loop's exit condition is ``get_event_loop().time() < deadline``
    and its only delay is ``asyncio.sleep(poll_interval)``. Stub either
    alone and the other still runs on real time — a no-op sleep against
    a real deadline spins, and a real sleep against a fake deadline never
    moves.

    So both come from a stand-in installed on ``qmd_daemon.asyncio``:
    the daemon module resolves that name as a module global, so the
    swap is visible to the recycle and to nothing else in the process.
    The clock advances one tick per poll, which makes the iteration count
    a function of ``ready_timeout`` and ``poll_interval`` rather than of
    how loaded the machine is.
    """
    import asyncio as real_asyncio

    state = {"sleeps": 0, "now": 0.0}

    class _Clock:
        def time(self) -> float:
            return state["now"]

    class _Shim:
        @staticmethod
        def get_event_loop() -> _Clock:
            return _Clock()

        @staticmethod
        async def sleep(seconds: float, *a: Any, **kw: Any) -> None:
            state["sleeps"] += 1
            state["now"] += max(seconds, tick_s)
            await real_asyncio.sleep(0)  # keep the loop fair; costs nothing

        def __getattr__(self, name: str) -> Any:  # proxy the rest
            return getattr(real_asyncio, name)

    monkeypatch.setattr(qmd_daemon, "asyncio", _Shim)
    return state


async def test_a_stopped_daemon_does_not_escape_the_recycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The measured dead-session shape is retried, not raised at the caller.

    Before the tuple was widened this propagated out of
    ``recycle_qmd_daemon`` on the very first probe: a bare ``RuntimeError``
    that never reached the function's own ``QmdRecycleFailed`` contract,
    spent none of ``ready_timeout``, and told the caller nothing about
    which daemon failed to come back.

    The teeth are in the assertions together: the loop must *retry*
    (many attempts, and one sleep between each), and it must converge on
    its own declared failure (``QmdRecycleFailed``). Narrowing the tuple
    back to its old three arms fails the first; swallowing everything
    without raising fails the second.
    """
    _stub_spawn_and_reap(monkeypatch)
    clock = _fake_clock(monkeypatch, tick_s=0.5)
    _DeadSessionClient.attempts = []
    monkeypatch.setattr("lies.qmd.daemon.fastmcp.Client", _DeadSessionClient)

    with pytest.raises(qmd_daemon.QmdRecycleFailed) as excinfo:
        await qmd_daemon.recycle_qmd_daemon(
            scope=tmp_path,
            daemon_url="http://127.0.0.1:8181/mcp",
            ready_timeout=30.0,
            poll_interval=0.5,
        )

    # A 30s budget at 0.5s per poll is 60 attempts. Asserted as a floor
    # rather than an equality: the fake clock accumulates floats, and
    # 0.1-style sums wobble by one iteration at the boundary. The teeth
    # are "many, and one sleep between each" — the pre-fix code managed
    # exactly one attempt and no sleep at all.
    assert len(_DeadSessionClient.attempts) >= 50
    # The loop is `while in-budget: probe; sleep`, so a run of failed
    # probes sleeps once per probe — the trailing sleep is what advances
    # the clock past the deadline. Pre-fix: one probe, one uncaught
    # exception, no sleep, no retry.
    assert clock["sleeps"] == len(_DeadSessionClient.attempts)
    assert excinfo.value.ready_timeout_s == 30.0
    assert "no listener" in excinfo.value.last_state.detail


def test_the_measured_exception_is_not_in_any_legacy_arm() -> None:
    """Why the old three arms could not have caught it.

    Not a tautology about the constant: it asserts a property of the
    *measured* exception that would have to be re-derived if fastmcp
    changed. If a future fastmcp stops wrapping, or starts raising
    something the old tuple already covers, this goes red and the
    comment above gets re-measured.
    """
    exc = _measured_dead_session_error()
    import httpx
    import mcp

    assert not isinstance(exc, (httpx.HTTPError, mcp.MCPError, OSError)), (
        "the dead-session RuntimeError now matches a legacy arm; the widened tuple may be reducible"
    )
    cause = exc.__cause__
    assert cause is not None
    assert not isinstance(cause, (httpx.HTTPError, OSError)), (
        "httpx2's transport error is catchable by the legacy arms after "
        "all — the wrap is not what is doing the work"
    )
    # Only asserted when httpx2 actually imported; the skip above is
    # unconditional, so reaching here means it is present.
    assert type(cause).__module__ == "httpx2"
    assert isinstance(exc, RuntimeError)
    assert RuntimeError in qmd_daemon._RECYCLE_PROBE_ERRORS


def test_the_widened_arm_cannot_swallow_our_own_failure_contract() -> None:
    """``RuntimeError`` is broad, and LIES' own failure is a ``RuntimeError``.

        Two facts make that safe, and both are load-bearing:

        1. ``QmdRecycleFailed`` subclasses ``RuntimeError``, so the widened
           arm *would* match it.
        2. Nothing inside the guarded ``try`` raises it. The ``raise`` is
           after the loop, outside the block.

        If a future edit moved that ``raise`` inside the ``try``, the loop
        would catch its own failure, log it as "probe failed", and spin to
        the budget instead of reporting — turning a clear failure into a
        silent wait.

    Asserted **structurally, over the AST**, not by comparing source
        line indices. An index comparison was tried first and is too weak:
        it stays green when a ``raise QmdRecycleFailed`` is added *inside*
        the try without moving the existing statement, which is the more
        likely edit - a "final check" inside the loop body.

        **What this does and does not enforce**, because a pin that reads
        stronger than it is worse than none. It matches the *bare* form
        only - ``raise QmdRecycleFailed(...)``, a ``Call`` whose func is a
        plain ``Name`` with that id. It does **not** see:

        - ``raise daemon.QmdRecycleFailed(...)`` (an ``Attribute`` func);
        - ``boom = QmdRecycleFailed(...); raise boom`` (a pre-built
          instance, where ``exc`` is a ``Name``);
        - a raise of some *other* ``RuntimeError`` subclass;
        - a raise reached by calling a helper from inside the try.

        Catching all of those means resolving names, which is the kind of
        inference that silently rots. The comment in ``daemon.py`` says the
        same thing, so neither the pin nor the prose overclaims.
    """
    import ast

    assert issubclass(qmd_daemon.QmdRecycleFailed, RuntimeError), (
        "QmdRecycleFailed is no longer a RuntimeError; the widened probe "
        "arm is no longer at risk of swallowing it - update the comment"
    )

    tree = ast.parse(Path(qmd_daemon.__file__).read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "recycle_qmd_daemon"
    )

    guarded: list[ast.Try] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            names = {h.type.id if isinstance(h.type, ast.Name) else None for h in [handler]}
            if names & {"_RECYCLE_PROBE_ERRORS"}:
                guarded.append(node)

    assert guarded, "the probe's guarded try/except is gone; re-derive this"

    def raises_bare_recycle_failed(node: ast.AST) -> bool:
        """The bare form only - see the docstring's 'does not enforce' list."""
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Raise)
                and isinstance(child.exc, ast.Call)
                and isinstance(child.exc.func, ast.Name)
                and child.exc.func.id == "QmdRecycleFailed"
            ):
                return True
        return False

    for node in guarded:
        assert not raises_bare_recycle_failed(node), (
            "a `raise QmdRecycleFailed(...)` now sits inside the try guarded "
            "by the widened RuntimeError arm; it would be caught, logged as a "
            "probe failure, and retried until the budget expires"
        )


async def test_a_serving_daemon_still_returns_on_the_first_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The happy path is untouched: one probe, no sleep, state returned.

    Guards against a fix that "works" by making the loop retry forever —
    the budget and the return value both have to survive.
    """
    _stub_spawn_and_reap(monkeypatch)
    clock = _fake_clock(monkeypatch, tick_s=0.5)
    monkeypatch.setattr(
        qmd_daemon,
        "qmd_daemon_state",
        lambda: qmd_daemon.QmdState(True, True, 4242, "running"),
    )

    built: list[str] = []

    class _Serving:
        def __init__(self, url: str) -> None:
            built.append(url)

        async def __aenter__(self) -> _Serving:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def list_tools(self) -> list[object]:
            return []

    monkeypatch.setattr("lies.qmd.daemon.fastmcp.Client", _Serving)

    state = await qmd_daemon.recycle_qmd_daemon(
        scope=tmp_path,
        daemon_url="http://127.0.0.1:8181/mcp",
        ready_timeout=30.0,
        poll_interval=0.5,
    )
    assert state.running is True and state.pid == 4242
    assert len(built) == 1
    assert clock["sleeps"] == 0


async def test_an_http_not_ready_response_is_still_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A booting daemon answering 404/503 arrives as ``MCPError``.

    Measured alongside the closed-port case. It was already caught by the
    old tuple and must stay caught: this is the arm that would break
    silently if a future edit narrowed the tuple to the transport shape
    alone.
    """
    import mcp

    _stub_spawn_and_reap(monkeypatch)
    _fake_clock(monkeypatch, tick_s=0.01)
    attempts: list[int] = []

    class _NotReady:
        def __init__(self, url: str) -> None:
            attempts.append(1)

        async def __aenter__(self) -> _NotReady:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def list_tools(self) -> list[object]:
            # `MCPError(code, message)` — the JSON-RPC error the streamable
            # -http layer raises for a 404/503 "not up yet" answer.
            raise mcp.MCPError(mcp.types.INTERNAL_ERROR, "not ready")

    monkeypatch.setattr("lies.qmd.daemon.fastmcp.Client", _NotReady)

    with pytest.raises(qmd_daemon.QmdRecycleFailed):
        await qmd_daemon.recycle_qmd_daemon(
            scope=tmp_path,
            daemon_url="http://127.0.0.1:8181/mcp",
            ready_timeout=0.1,
            poll_interval=0.01,
        )
    assert len(attempts) >= 8  # 0.1s budget at 0.01s per poll is ~10
    assert mcp.MCPError in qmd_daemon._RECYCLE_PROBE_ERRORS


def test_the_docstring_states_the_measured_floor_and_ceiling() -> None:
    """The worst case is a floor and a ceiling, both from measurement.

    The docstring's history is a useful record of how wrong these
    figures have been: a single "45 s" that summed mutually exclusive
    component bounds, then a "~31 s / ~80 s" pair where the ceiling was
    **unreachable** — a hanging ``qmd status`` makes ``qmd_daemon_state``
    report ``running=False``, so ``_reap_qmd_daemon`` skips both grace
    windows and the 4 s the old figure leaned on is exactly what the
    slow-status path forgoes. The spawn's own 15 s bound dominated the
    middle and was named by neither.

    Asserting the literals so the arithmetic has to be edited in two
    places. Whitespace is collapsed first: this is prose, and prose gets
    re-wrapped by the formatter without anyone changing what it says.
    """
    doc = " ".join((qmd_daemon.recycle_qmd_daemon.__doc__ or "").split())

    assert "31.0 s" in doc, "the measured floor must be stated"
    assert "65.8 s" in doc, "the measured ceiling must be stated; ~80s was not an upper bound"
    assert "47.5 s" in doc and "48.6 s" in doc, (
        "both single-bound scenarios must be tabulated; the spawn-hang "
        "case is the dominant intermediate and is what the old figures missed"
    )
    assert "unreachable" in doc, (
        "the docstring must record that the earlier ~80s ceiling was "
        "unreachable, not merely loose — that is why the shape changed"
    )
    assert "dominant" in doc and "spawn" in doc, "the spawn-hang case must be named as dominant"
    assert "ever been observed" in doc, (
        "the docstring must keep recording that none of this was "
        "reachable before the except tuple was widened"
    )


def test_the_ceiling_is_a_measured_range_not_a_single_figure() -> None:
    """No lone single figure may replace the table.

    Two figures have now been wrong on this exact line — "45 s" and
    "~80 s" — and both were single numbers presented as bounds. A
    regression to one number is the failure mode worth pinning, and it
    is cheap to check: a bound this path has is a range, so the docstring
    must still name both ends of it.
    """
    doc = " ".join((qmd_daemon.recycle_qmd_daemon.__doc__ or "").split())

    assert "floor" in doc and "ceiling" in doc, (
        "the docstring must frame the bound as a range with named ends"
    )
    # "45 s" must not appear as a live bound. It may appear in the
    # history paragraph, which says it was wrong; that is allowed, and
    # distinguishing the two is the point.
    for sentence in doc.split(". "):
        if "45 s" in sentence:
            assert any(word in sentence for word in ("wrong", "summed", "replaces", "mutually")), (
                f"'45 s' reads as a live bound here: {sentence!r}"
            )


async def test_the_recycle_spawns_on_the_port_it_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The restart lands on the port the readiness probe is watching.

    The bug this pins is a live one, not a hypothetical. Against
    ``LIES_QMD_URL=http://127.0.0.1:9000/mcp``: the reap correctly
    kills the 9000 daemon, a spawn without ``--port`` brings up a
    **stray on 8181** that also overwrites ``mcp.pid``, and the probe
    then polls 9000 for its whole budget. The operator gets
    ``QmdRecycleFailed`` having lost their daemon and gained a squatting
    one — and ``qmd.js:4008-4013`` then refuses to start a real daemon,
    because the stray's pidfile is live.

    So the port is parsed out of ``daemon_url``, not defaulted.

    The probe is stubbed (via the module's ``_no_socket_to_loopback``
    guard, this test previously opened a real socket to the port it
    names — see that fixture).
    """
    _stub_spawn_and_reap(monkeypatch)
    _fake_clock(monkeypatch, tick_s=0.5)
    _DeadSessionClient.attempts = []
    monkeypatch.setattr("lies.qmd.daemon.fastmcp.Client", _DeadSessionClient)
    spawned: list[dict[str, object]] = []

    def _capture(**kw: object) -> None:
        spawned.append(kw)

    monkeypatch.setattr(qmd_daemon, "_spawn_qmd_daemon", _capture)

    with pytest.raises(qmd_daemon.QmdRecycleFailed):
        await qmd_daemon.recycle_qmd_daemon(
            scope=tmp_path,
            daemon_url="http://127.0.0.1:9000/mcp",
            ready_timeout=0.1,
            poll_interval=0.01,
        )

    assert spawned == [{"port": 9000}], (
        f"the recycle must restart the daemon on the port it probes; spawned {spawned}"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:9000/mcp", 9000),
        ("http://127.0.0.1:8181/mcp", 8181),
        ("https://example.test:443/mcp", 443),
        # No explicit port: "let qmd default", which is not port 0 and
        # not an assumption that the default is 8181.
        ("http://127.0.0.1/mcp", None),
        ("127.0.0.1:9000", 9000),
        ("http://127.0.0.1:notaport/mcp", None),
    ],
)
def test_the_port_is_read_from_the_url(url: str, expected: int | None) -> None:
    assert qmd_daemon.daemon_port_from_url(url) == expected
