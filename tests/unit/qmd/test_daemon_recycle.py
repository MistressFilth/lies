"""The recycle readiness probe: what it retries on, and what it costs.

Every test here is stub-only and sleeps zero real seconds — the
``asyncio.sleep`` between attempts is replaced by a counter-driven no-op
and ``ready_timeout`` is set in the milliseconds. Spawning a real qmd
daemon is not this file's job; see
``tests/integration/test_qmd_spawn_throwaway.py``.
"""

from __future__ import annotations

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


def _measured_dead_session_error() -> RuntimeError:
    """The exact exception ``fastmcp.Client(url).list_tools()`` raises."""
    import httpx2  # fastmcp's vendored httpx; the shape of the __cause__

    try:
        raise httpx2.ConnectError("All connection attempts failed")
    except httpx2.ConnectError as cause:
        err = RuntimeError("Client failed to connect: All connection attempts failed")
        err.__cause__ = cause
        return err


class _DeadSessionClient:
    """A ``fastmcp.Client`` stand-in for a daemon that is not serving.

    Every construction raises the measured shape — that is what a closed
    port does — and counts its own attempts so the test can prove the
    loop retried instead of escaping.
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


def _stub_spawn_and_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence everything in the recycle except its probe loop."""
    monkeypatch.setattr(qmd_daemon, "_reap_qmd_daemon", lambda **kw: None)
    monkeypatch.setattr(qmd_daemon, "_spawn_qmd_daemon", lambda **kw: None)
    monkeypatch.setattr(qmd_daemon, "write_sidecar_data_dir", lambda data_dir: None)
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
            data_dir=tmp_path,
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
    assert type(cause).__module__ == "httpx2"
    assert isinstance(exc, RuntimeError)
    assert RuntimeError in qmd_daemon._RECYCLE_PROBE_ERRORS


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
        data_dir=tmp_path,
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
            data_dir=tmp_path,
            daemon_url="http://127.0.0.1:8181/mcp",
            ready_timeout=0.1,
            poll_interval=0.01,
        )
    assert len(attempts) >= 8  # 0.1s budget at 0.01s per poll is ~10
    assert mcp.MCPError in qmd_daemon._RECYCLE_PROBE_ERRORS


def test_the_docstring_states_a_reachable_worst_case() -> None:
    """The recycle's ceiling is documented, and it is the budget.

    The docs said "tens of seconds" while the loop escaped in ~0.3 s, so
    the figure described a path no one had walked. A docstring that
    loses the ceiling is how that came to happen.
    """
    doc = qmd_daemon.recycle_qmd_daemon.__doc__ or ""
    assert "ready_timeout" in doc
    assert "45 s" in doc, "the worst case must be stated as a number"
    assert "never been observed" in doc, (
        "the docstring must keep recording that the ceiling was "
        "unreachable before the except tuple was widened"
    )
