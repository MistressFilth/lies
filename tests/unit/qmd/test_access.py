"""Unit tests for ``lies.qmd.access`` — the qmd access seam.

The seam owns every call LIES makes to qmd and encodes which transport
serves which operation. These tests are all I/O-free: the daemon probe,
the staleness check, the client, the recycle, and the daemon log are
stubbed, because the behaviour under test is the *decision* (which
transport, which recovery, which exception) and not qmd's answers.

Error taxonomy under test:

- probe fails            → ``QmdDaemonUnavailable`` naming ``LIES_QMD_URL``
                           and ``lies qmd up``. Never a fallback.
- daemon stale           → reap + respawn, then proceed. Not an error.
- ``httpx.ReadTimeout``  → recycle, raise ``QmdDaemonWedged`` carrying the
                           daemon log's tail. No transparent retry: a fresh
                           daemon re-wedges on the same payload.
- transport error        → recycle, retry once; a second failure raises.
- ``isError: true``      → ``RuntimeError`` carrying the daemon's text.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import mcp
import pytest
from fastmcp.client.client import CallToolResult
from mcp.types import TextContent

from lies.qmd import access
from lies.qmd.daemon import QmdRecycleFailed, QmdState


class _FakeClient:
    """Stands in for the cached ``fastmcp.Client``.

    A real Client needs a connected MCP session; the seam only needs
    something it can ``async with`` and call ``call_tool`` on. Each
    entry in ``outcomes`` is either an exception to raise or a value to
    return; the last one repeats once the list is exhausted.

    Records every ``call_tool`` invocation in ``calls`` with the
    full kwargs so a test can pin the per-call ``timeout=`` argument
    is threaded through. ``_FakeClient.call_tool`` is the only place
    the seam's deadline lands on the wire, so an assertion on the
    captured kwargs is the assertion on the property the
    ``LIES_QMD_FANOUT_TIMEOUT`` knob depends on.
    """

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        self.calls.append((name, arguments, kw))
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _tool_error(text: str) -> CallToolResult:
    """A result carrying qmd's own error text, in the real result shape."""
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=None,
        meta=None,
        data=None,
        is_error=True,
    )


class _Recycle:
    """Records the kwargs each recycle was called with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> QmdState:
        self.calls.append(kwargs)
        return QmdState(installed=True, running=True, pid=4242, detail="fresh")


@pytest.fixture(autouse=True)
def _reset_cached_client() -> Any:
    """Keep the module-level client cache from leaking between tests.

    The cache exists so the daemon's model stays warm across calls; a
    test that left a client behind would hand the next one a stub.
    """
    access._reset_client()
    yield
    access._reset_client()


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> _Recycle:
    """A reachable, non-stale daemon whose recycle is recorded.

    Every test that gets past the probe starts here, so the individual
    tests read as the one behaviour they are about rather than four
    lines of stubbing each.
    """
    monkeypatch.setattr(access, "qmd_daemon_reachable", lambda url, timeout=0.5: True)
    monkeypatch.setattr(access, "_is_daemon_stale", lambda: False)
    # The sidecar is a real file under $HOME; a test must not read it.
    monkeypatch.setattr(access, "_recycle_data_dir", lambda: Path("/nonexistent"))
    recycle = _Recycle()
    monkeypatch.setattr(access, "recycle_qmd_daemon", recycle)
    return recycle


def _use_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr(access, "_daemon_client", lambda url: client)


# --- the capability map ------------------------------------------------


def test_the_map_covers_exactly_what_each_transport_exposes() -> None:
    assert access.DAEMON_TOOLS == {"query", "get", "multi_get", "status"}
    assert "search" in access.CLI_ONLY_OPS  # the daemon has no BM25 path
    assert not (access.DAEMON_TOOLS & access.CLI_ONLY_OPS)


def test_maintenance_and_lifecycle_stay_on_the_cli() -> None:
    # The daemon serves reads and nothing else; these are the operations
    # that keep the index itself alive, and none of them is a tool call.
    assert access.CLI_ONLY_OPS >= {"update", "embed", "cleanup", "collection", "mcp"}


async def test_a_cli_only_op_is_refused_before_it_reaches_the_daemon() -> None:
    # `search` is BM25. The daemon has no such tool, and a call that
    # reached it anyway would come back empty — which reads downstream
    # as "the corpus has nothing".
    with pytest.raises(ValueError, match="CLI_ONLY"):
        await access.daemon_tool("search", {"query": "hooks"})


# --- the down path -----------------------------------------------------


async def test_a_down_daemon_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access, "qmd_daemon_reachable", lambda url, timeout=0.5: False)

    def _no_client(url: str) -> Any:
        raise AssertionError("a down daemon must not reach the client")

    monkeypatch.setattr(access, "_daemon_client", _no_client)

    with pytest.raises(access.QmdDaemonUnavailable) as e:
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    # Both halves of the fix are named: the command that starts the
    # daemon, and the variable that says where it is. `lies mcp up` is
    # LIES' own MCP server — a different daemon, and following it would
    # change nothing.
    assert "lies qmd up" in str(e.value)
    assert "LIES_QMD_URL" in str(e.value)


def test_both_daemon_failures_are_runtime_errors() -> None:
    # Callers catch one type at the boundary (Task 4's envelope, Task 3's
    # read path); two unrelated names for one condition is one too many.
    assert issubclass(access.QmdDaemonUnavailable, RuntimeError)
    assert issubclass(access.QmdDaemonWedged, RuntimeError)


# --- staleness is not an error ----------------------------------------


async def test_a_stale_daemon_is_reaped_and_the_call_still_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaped: list[bool] = []
    spawned: list[bool] = []
    monkeypatch.setattr(access, "qmd_daemon_reachable", lambda url, timeout=0.5: True)
    monkeypatch.setattr(access, "_is_daemon_stale", lambda: True)
    monkeypatch.setattr(access, "_reap_qmd_daemon", lambda: reaped.append(True))
    monkeypatch.setattr(access, "_spawn_qmd_daemon", lambda: spawned.append(True))
    client = _FakeClient([{"hits": ["served-after-respawn"]}])
    _use_client(monkeypatch, client)

    result = await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    assert result == {"hits": ["served-after-respawn"]}
    assert reaped == [True]
    assert spawned == [True]


# --- the wedged path ---------------------------------------------------


async def test_a_wedged_daemon_recycles_then_raises_with_the_log_tail(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    monkeypatch.setattr(access, "_daemon_log_tail", lambda: "expanding query 3/7")
    client = _FakeClient([httpx.ReadTimeout("wedged")])
    _use_client(monkeypatch, client)

    with pytest.raises(access.QmdDaemonWedged) as e:
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    # The log tail is the evidence. A wedge reported as nothing but a
    # fired deadline tells the reader only what they already knew.
    assert e.value.last_output == "expanding query 3/7"
    assert len(seam.calls) == 1
    # No transparent retry: a fresh daemon re-wedges on the same payload.
    assert len(client.calls) == 1


async def test_the_wedge_carries_the_log_as_it_was_before_the_recycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seam: _Recycle,
) -> None:
    """The tail must be read before the recycle, not after.

    `recycle_qmd_daemon` is reap+spawn, and qmd truncates `mcp.log` on
    every start — so a tail read after the recycle describes the
    *replacement* daemon. This test's stub does exactly what qmd does:
    it truncates the log when the recycle runs. Asserting against a
    constant stub (as the test above does) pins the wiring but not the
    ordering, and passes on either order; replacing the file is the only
    shape that fails when the read moves after the recycle.
    """
    log = tmp_path / "mcp.log"
    log.write_text("expanding query 3/7\n")

    async def _recycle_that_truncates(**_kwargs: Any) -> QmdState:
        log.write_text("")  # qmd truncates mcp.log on every daemon start
        return QmdState(installed=True, running=True, pid=4242, detail="fresh")

    # The `seam` fixture supplies the probe, the staleness check, and
    # the `_recycle_data_dir` stub — the last of which keeps
    # `read_sidecar_data_dir()` from reaching a real $HOME. Only the
    # recycle itself is replaced, because the truncation is the point.
    monkeypatch.setattr(access, "recycle_qmd_daemon", _recycle_that_truncates)
    monkeypatch.setattr(access, "_daemon_log_tail", lambda: log.read_text().strip())
    _use_client(monkeypatch, _FakeClient([httpx.ReadTimeout("wedged")]))

    with pytest.raises(access.QmdDaemonWedged) as e:
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    assert e.value.last_output == "expanding query 3/7"
    assert log.read_text() == "", "the stub did not model qmd's truncation"


async def test_the_wedge_message_names_the_tool(
    monkeypatch: pytest.MonkeyPatch, seam: _Recycle
) -> None:
    monkeypatch.setattr(access, "_daemon_log_tail", lambda: "")
    _use_client(monkeypatch, _FakeClient([httpx.ReadTimeout("wedged")]))

    with pytest.raises(access.QmdDaemonWedged) as e:
        await access.daemon_tool("get", {"file": "claude_code/hooks.md"})

    assert "get" in str(e.value)


def test_the_wedge_carries_the_tail_of_the_daemons_own_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = tmp_path / "mcp.log"
    log.write_text("".join(f"line {i}\n" for i in range(500)))

    monkeypatch.setattr("lies.qmd.lifecycle._logfile", lambda: log)
    tail = access._daemon_log_tail()

    # Bounded: the log grows for the life of the daemon, and the wedge
    # exception is a message, not an index export.
    assert len(tail) < 4000
    assert tail.endswith("line 499")
    assert "line 0\n" not in tail


def test_a_missing_daemon_log_leaves_the_wedge_without_its_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("lies.qmd.lifecycle._logfile", lambda: tmp_path / "absent.log")

    # The wedge is still worth reporting; it just arrives without the
    # evidence, so this must not raise.
    assert access._daemon_log_tail() == ""


async def test_a_wedge_survives_a_recycle_that_never_served(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    # A failed restart must not replace the diagnosis. The caller
    # observed a wedge; a `recycle` that never served is a second fact
    # about it, not a different failure to report.
    async def _failed_recycle(**_kwargs: Any) -> QmdState:
        raise QmdRecycleFailed(30.0, QmdState(False, False, None, "stuck"))

    monkeypatch.setattr(access, "recycle_qmd_daemon", _failed_recycle)
    _use_client(monkeypatch, _FakeClient([httpx.ReadTimeout("wedged")]))

    with pytest.raises(access.QmdDaemonWedged):
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})


async def test_a_failed_recycle_tells_the_operator_the_daemon_is_down(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    """A recycle that never served leaves a machine-global daemon stopped.

    The wedge message said "recycled" unconditionally, so a restart
    that exhausted its budget told the operator the opposite of what
    happened -- and named no command. The diagnosis stays a wedge; the
    operator-actionable fact rides with it, because that is the fact
    the next action depends on and it is the one that was being lost.
    """

    async def _failed_recycle(**_kwargs: Any) -> QmdState:
        raise QmdRecycleFailed(30.0, QmdState(False, False, None, "stuck"))

    monkeypatch.setattr(access, "recycle_qmd_daemon", _failed_recycle)
    _use_client(monkeypatch, _FakeClient([httpx.ReadTimeout("wedged")]))

    with pytest.raises(access.QmdDaemonWedged) as excinfo:
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    message = str(excinfo.value)
    assert "recycled," not in message, (
        "the message asserts a successful recycle; it did not succeed"
    )
    assert "lies qmd up" in message, (
        f"a stopped machine-global daemon names no way to start it; got {message!r}"
    )


async def test_a_failed_recycle_still_logs_the_exhausted_budget(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure is loud in the log as well as in the raised error.

    A caller that catches ``QmdDaemonWedged`` and retries -- which is
    a reasonable thing to do -- gets no console trace of the daemon
    being down. The log is the only place that fact survives.
    """

    async def _failed_recycle(**_kwargs: Any) -> QmdState:
        raise QmdRecycleFailed(30.0, QmdState(False, False, None, "stuck"))

    monkeypatch.setattr(access, "recycle_qmd_daemon", _failed_recycle)
    _use_client(monkeypatch, _FakeClient([httpx.ReadTimeout("wedged")]))

    with caplog.at_level(logging.ERROR, logger="lies.qmd.access"):
        with pytest.raises(access.QmdDaemonWedged):
            await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    assert any(
        "recycle" in r.getMessage() and "lies qmd up" in r.getMessage() for r in caplog.records
    ), f"the stopped daemon must be logged; got {[r.getMessage() for r in caplog.records]!r}"


# --- the transport-error path ------------------------------------------


async def test_a_transport_error_recycles_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    client = _FakeClient([httpx.ConnectError("refused"), {"hits": ["after-retry"]}])
    _use_client(monkeypatch, client)

    result = await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    assert result == {"hits": ["after-retry"]}
    assert len(seam.calls) == 1
    assert len(client.calls) == 2


async def test_the_retryable_path_never_reads_the_daemon_log(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    """A transport error carries no tail, so it must not read one.

    The wedge is the only failure that reports a log tail. Reading it
    on the retryable path would spend an open+seek+read of qmd's log on
    a value that is thrown away — on the *common* path, since a daemon
    that is restarting is ordinary rather than exceptional. It also put
    a real filesystem read behind a test module that documents itself as
    I/O-free, which is how a test ends up reading someone's home
    directory the day the XDG isolation changes.
    """
    reads: list[bool] = []
    monkeypatch.setattr(access, "_daemon_log_tail", lambda: reads.append(True) or "never used")
    client = _FakeClient([httpx.ConnectError("refused"), {"hits": ["after-retry"]}])
    _use_client(monkeypatch, client)

    result = await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    assert result == {"hits": ["after-retry"]}
    assert reads == []


async def test_a_transport_error_that_survives_a_recycle_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    client = _FakeClient([httpx.ConnectError("refused"), httpx.ConnectError("still down")])
    _use_client(monkeypatch, client)

    with pytest.raises(access.QmdDaemonUnavailable) as e:
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    # After a recycle the daemon is down, not wedged: the two demand
    # different responses from the operator.
    assert "lies qmd up" in str(e.value)
    assert len(client.calls) == 2  # one retry, never a third attempt
    assert len(seam.calls) == 1


async def test_a_retry_failing_for_an_unowned_reason_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    # A decode error on the retry says nothing about the daemon's
    # reachability. Wrapping it in QmdDaemonUnavailable would assert
    # "not serving" about a daemon that was serving.
    err = json.JSONDecodeError("Expecting value", "<html>", 0)
    client = _FakeClient([httpx.ConnectError("refused"), err])
    _use_client(monkeypatch, client)

    with pytest.raises(json.JSONDecodeError) as e:
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    assert e.value is err
    assert len(client.calls) == 2  # the retry still happened


async def test_a_retry_that_wedges_reports_a_wedge_not_a_down_daemon(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    monkeypatch.setattr(access, "_daemon_log_tail", lambda: "expanding query 1/9")
    client = _FakeClient([httpx.ConnectError("refused"), httpx.ReadTimeout("wedged again")])
    _use_client(monkeypatch, client)

    with pytest.raises(access.QmdDaemonWedged) as e:
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    # A daemon that answers the connection and then stalls is wedged
    # again, not down — and the log tail is still the useful evidence.
    assert e.value.last_output == "expanding query 1/9"


async def test_each_wedge_carries_the_log_of_the_daemon_that_actually_wedged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seam: _Recycle,
) -> None:
    """Both tail reads are correct, and they are correct for opposite reasons.

    Two branches report a wedge, and each reads the log on the other
    side of its recycle:

    - the first call wedges — D1 wedged, and the recycle below spawns
      its replacement D2, which truncates mcp.log. The read precedes
      the recycle, so the tail is D1's.
    - a retry wedges — the recycle already spawned D2 and the retry ran
      against it, so the read follows that recycle and the tail is D2's.

    One daemon log stands in for both; each recycle rewrites it with the
    identity of the daemon it started. A read on the wrong side
    therefore does not produce a missing tail but a *misattributed*
    one, which is the direction this field exists to prevent — so the
    test asserts which daemon's words arrived, not merely that some
    text did.

    The `seam` parameter is load-bearing even though its recorder goes
    unused: the test replaces the recycle because it has to control
    *when* the log is truncated, not *what* the recycle does, and the
    fixture is what stubs the daemon probe, the staleness check, and
    `_recycle_data_dir`. Drop it as vestigial and `read_sidecar_data_dir`
    runs unstubbed, reaching `library_git_root()` → `Library.open()`
    against the real filesystem — contained today only by the autouse
    XDG isolation in `tests/conftest.py`.
    """
    log = tmp_path / "mcp.log"
    log.write_text("D1: expanding query 3/7")
    generation = {"n": 1}

    async def _recycle_spawns_the_next_daemon(**_kwargs: Any) -> QmdState:
        # qmd truncates mcp.log on every start; the replacement writes
        # its own.
        generation["n"] += 1
        log.write_text(f"D{generation['n']}: expanding query 1/2")
        return QmdState(installed=True, running=True, pid=generation["n"], detail="fresh")

    monkeypatch.setattr(access, "recycle_qmd_daemon", _recycle_spawns_the_next_daemon)
    monkeypatch.setattr(access, "_daemon_log_tail", lambda: log.read_text())
    _use_client(monkeypatch, _FakeClient([httpx.ReadTimeout("wedged")]))

    # The first call wedges. D1 is the daemon that wedged, and the
    # recycle that follows only starts D2 — the tail must be D1's.
    with pytest.raises(access.QmdDaemonWedged) as first:
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})
    assert first.value.last_output == "D1: expanding query 3/7"

    # A transport error, then a retry that wedges. D2 is the daemon that
    # wedged, and it has been logging since the recycle started it — the
    # tail must be D2's, not D1's pre-recycle log.
    access._reset_client()
    _use_client(
        monkeypatch,
        _FakeClient([httpx.ConnectError("refused"), httpx.ReadTimeout("wedged again")]),
    )
    generation["n"] = 1
    log.write_text("D1: starting up")
    with pytest.raises(access.QmdDaemonWedged) as second:
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})
    assert second.value.last_output == "D2: expanding query 1/2"


async def test_a_wrapped_connect_timeout_is_recycled_not_leaked_as_a_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    # fastmcp wraps httpx.ConnectTimeout in an mcp.MCPError. Unwrapped,
    # it reads as a protocol rejection and the daemon is never restarted.
    client = _FakeClient(
        [
            mcp.MCPError(code=httpx.codes.REQUEST_TIMEOUT, message="Timed out."),
            {"hits": ["after-retry"]},
        ]
    )
    _use_client(monkeypatch, client)

    result = await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    assert result == {"hits": ["after-retry"]}
    assert len(seam.calls) == 1


# --- the tool-error path -----------------------------------------------


async def test_a_tool_error_result_raises_with_the_daemons_own_text(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    _use_client(monkeypatch, _FakeClient([_tool_error("query failed: index not loaded")]))

    with pytest.raises(RuntimeError) as e:
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    assert "query failed: index not loaded" in str(e.value)
    # A tool error is neither a wedge nor a transport failure, so it is
    # never recycled — a restart cannot change the daemon's answer.
    assert not isinstance(e.value, access.QmdDaemonWedged)
    assert seam.calls == []


async def test_a_protocol_rejection_passes_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    err = mcp.MCPError(code=-32602, message="Invalid params")
    _use_client(monkeypatch, _FakeClient([err]))

    with pytest.raises(mcp.MCPError) as e:
        await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})

    assert e.value is err
    assert seam.calls == []


# --- the cached client -------------------------------------------------


async def test_the_client_is_built_once_and_reused_across_calls(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    built: list[str] = []
    fake = _FakeClient([{"ok": 1}, {"ok": 2}])

    def _factory(url: str) -> _FakeClient:
        built.append(url)
        return fake

    monkeypatch.setattr(access, "fastmcp", SimpleNamespace(Client=_factory))
    monkeypatch.setattr(access, "get_qmd_url", lambda: "http://127.0.0.1:8181")

    first = await access.daemon_tool("query", {"searches": [{"type": "lex", "query": "x"}]})
    second = await access.daemon_tool("get", {"file": "a.md"})

    assert (first, second) == ({"ok": 1}, {"ok": 2})
    assert len(built) == 1, "the daemon's model is warm; the client must be too"


async def test_a_changed_daemon_url_rebuilds_the_client(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    built: list[str] = []
    monkeypatch.setattr(
        access,
        "fastmcp",
        SimpleNamespace(Client=lambda url: built.append(url) or _FakeClient([{"ok": 1}])),
    )
    monkeypatch.setattr(access, "get_qmd_url", lambda: "http://127.0.0.1:8181")
    await access.daemon_tool("get", {"file": "a.md"})

    monkeypatch.setattr(access, "get_qmd_url", lambda: "http://127.0.0.1:9999")
    await access.daemon_tool("get", {"file": "a.md"})

    assert len(built) == 2, "a cached client would keep calling the old daemon"


async def test_a_per_call_timeout_is_forwarded_to_call_tool(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    """``daemon_tool(..., timeout=N)`` reaches the wire as ``call_tool(timeout=N)``.

    The pre-final-review seam took a ``timeout`` argument at the search
    call site and never read it, so a deadline the CLI path would have
    applied was silently dropped on the daemon path. The cache invalidates
    on ``LIES_QMD_URL`` change but not on a deadline change, so
    ``LIES_QMD_FANOUT_TIMEOUT`` had no effect on the daemon transport
    until something else rebuilt the client. This test pins the property
    the knob depends on: the per-call timeout reaches ``call_tool`` as
    a keyword argument, so ``fastmcp.Client``'s per-request
    ``read_timeout_seconds`` honours it on the next call.
    """
    fake = _FakeClient([{"ok": 1}, {"ok": 2}])
    monkeypatch.setattr(access, "_daemon_client", lambda url: fake)
    monkeypatch.setattr(access, "get_qmd_url", lambda: "http://127.0.0.1:8181")

    await access.daemon_tool("get", {"file": "a.md"}, timeout=7.5)
    await access.daemon_tool("get", {"file": "b.md"}, timeout=12.0)

    # Two calls recorded, each with the per-call timeout threaded
    # through. ``raise_on_error=False`` is the seam's own choice (see
    # ``_call``); the assertion is the timeout, not that flag.
    assert len(fake.calls) == 2
    for call in fake.calls:
        assert call[2].get("timeout") is not None, (
            f"per-call timeout did not reach call_tool kwargs: {call[2]!r}"
        )
    assert fake.calls[0][2]["timeout"] == 7.5
    assert fake.calls[1][2]["timeout"] == 12.0


async def test_no_per_call_timeout_falls_back_to_the_cached_client_deadline(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    """A ``timeout=None`` call leaves the client-level deadline in place.

    The cached httpx client is built at factory construction with
    ``read=_default_read_timeout_s()``. ``daemon_tool(timeout=None)``
    must NOT pass ``timeout=None`` to ``call_tool`` — the FastMCP
    signature accepts it (no-op) but it would be confusing in the
    wire capture and would lie to a reader about whether the seam
    intends to override. The seam forwards ``None`` as "don't
    override"; the assertion pins the seam's choice.
    """
    fake = _FakeClient([{"ok": 1}])
    monkeypatch.setattr(access, "_daemon_client", lambda url: fake)
    monkeypatch.setattr(access, "get_qmd_url", lambda: "http://127.0.0.1:8181")

    await access.daemon_tool("get", {"file": "a.md"})

    assert len(fake.calls) == 1
    # The keyword is absent; the cached client's read deadline is the
    # one that applies. ``"timeout" not in kwargs`` is what
    # ``Client.call_tool`` reads as "use my default".
    assert "timeout" not in fake.calls[0][2], (
        f"a None timeout should not be forwarded as a kwarg; got {fake.calls[0][2]!r}"
    )


async def test_a_deadline_change_does_not_rebuild_the_cached_client(
    monkeypatch: pytest.MonkeyPatch,
    seam: _Recycle,
) -> None:
    """Cache key is the URL. Deadline moves on the next call, not via rebuild.

    A deadline change (``LIES_QMD_FANOUT_TIMEOUT``) is now read on the
    next call via the per-call ``timeout=`` argument, so the cached
    client can outlive a deadline change. This test pins that: a
    call with a different timeout leaves the cached client alone.
    A future change that adds the deadline to the cache key would
    rebuild the client on every deadline change and re-pay the
    model-load cost (3.11s warm vs 10.77s cold) for nothing.
    """
    built: list[str] = []
    fake = _FakeClient([{"ok": 1}, {"ok": 2}])

    def _factory(url: str) -> _FakeClient:
        built.append(url)
        return fake

    monkeypatch.setattr(access, "fastmcp", SimpleNamespace(Client=_factory))
    monkeypatch.setattr(access, "get_qmd_url", lambda: "http://127.0.0.1:8181")

    await access.daemon_tool("get", {"file": "a.md"}, timeout=7.5)
    await access.daemon_tool("get", {"file": "b.md"}, timeout=12.0)

    assert len(built) == 1, (
        f"a deadline change should not rebuild the cached client; "
        f"got {len(built)} builds. Rebuilding re-pays the 3.11s "
        f"model-load cost for nothing — the per-call timeout "
        f"overrides on the wire."
    )


# --- the classification ------------------------------------------------


def test_a_read_timeout_recycles_and_does_not_retry() -> None:
    assert access.classify_call_error(httpx.ReadTimeout("x")) == ("recycle-raise", False)


def test_a_transport_error_classifies_as_retry() -> None:
    assert access.classify_call_error(httpx.ConnectError("x")) == ("recycle-retry", True)


def test_a_wrapped_connect_timeout_recycles_and_retries_once() -> None:
    err = mcp.MCPError(code=httpx.codes.REQUEST_TIMEOUT, message="Timed out.")
    assert access.classify_call_error(err) == ("recycle-retry", True)


def test_a_protocol_rejection_is_not_ours_to_recycle() -> None:
    err = mcp.MCPError(code=-32602, message="Invalid params")
    assert access.classify_call_error(err) == ("passthrough", False)


# --- the protocol-error split (I-4) -------------------------------------
#
# ``LocalProtocolError`` and ``RemoteProtocolError`` both inherit
# ``ProtocolError -> TransportError`` in the installed httpx 0.28.1, so
# the generic ``_TRANSPORT_NAMES`` matcher would lump them into
# ``recycle-retry`` even though the failures live on opposite sides of
# the wire. The split pins the project's stated principle: server-side
# state -> recycle; client-side malformed request -> passthrough, since
# a retry against the same bytes fails the same way and a recycle
# kills in-flight work belonging to other clients of a machine-global
# daemon. ``HTTPStatusError`` is its own server-state passthrough.
# All three classes are pinned here so a future match-by-name rewrite
# cannot silently re-merge the client-side case.


def test_http_status_error_is_a_passthrough() -> None:
    """5xx from a daemon is a server-state response, not a transport failure."""
    req = httpx.Request("GET", "http://example.test/q")
    resp = httpx.Response(500, request=req)
    err = httpx.HTTPStatusError("500", request=req, response=resp)
    assert access.classify_call_error(err) == ("passthrough", False)


def test_remote_protocol_error_stays_a_recycle_retry() -> None:
    """The daemon's response was malformed — server-side state."""
    assert access.classify_call_error(httpx.RemoteProtocolError("x")) == ("recycle-retry", True)


def test_local_protocol_error_is_a_passthrough_not_a_recycle_retry() -> None:
    """A client-side malformed request cannot be fixed by restarting the daemon.

    ``httpx`` raises ``LocalProtocolError`` for illegal header
    values, unsupported URL schemes, and similar client-side
    problems. The daemon is not at fault; recycling it kills
    in-flight work for other clients of a machine-global daemon,
    and a retry sends the same bytes back to fail the same way.
    """
    assert access.classify_call_error(httpx.LocalProtocolError("x")) == ("passthrough", False)


def test_local_protocol_error_wrapped_by_fastmcp_still_classifies_as_passthrough() -> None:
    """The ``__cause__`` chain still resolves to the client-side class."""
    wrapped = RuntimeError("Client failed to connect: bad request")
    wrapped.__cause__ = httpx.LocalProtocolError("illegal header value")

    assert access.classify_call_error(wrapped) == ("passthrough", False)


# --- the shared scope-pre-check (I-6) ------------------------------------
#
# ``search`` and ``ground`` both issue ``query`` calls with a batched
# ``collections`` array. The daemon answers an unknown collection
# with an empty result and **no error**, so a single unresolvable
# name in the batch silently returns zero rows. ``validate_scope``
# is the shared pre-check that closes the class. Pinning both
# branches here means a future call site that ships a new batched
# tool cannot forget the pre-check.


def test_validate_scope_returns_input_for_empty_scope() -> None:
    """No scope in, no scope out — and no daemon call."""
    assert asyncio.run(access.validate_scope([])) == ([], [])


def test_validate_scope_separates_known_from_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``validate_scope`` partitions input order into served + absent."""

    async def _status(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        assert name == "status"
        return SimpleNamespace(
            content=[],
            structured_content={"collections": ["alpha", "beta"]},
            is_error=False,
        )

    monkeypatch.setattr(access, "daemon_tool", _status)

    validated, unknown = asyncio.run(access.validate_scope(["alpha", "gamma", "beta"]))
    assert validated == ["alpha", "beta"]
    assert unknown == ["gamma"]


def test_validate_scope_accepts_dict_shaped_collections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon's structured ``collections`` can be ``list[dict]`` too."""

    async def _status(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        return SimpleNamespace(
            content=[],
            structured_content={"collections": [{"name": "alpha"}]},
            is_error=False,
        )

    monkeypatch.setattr(access, "daemon_tool", _status)

    validated, unknown = asyncio.run(access.validate_scope(["alpha", "beta"]))
    assert validated == ["alpha"]
    assert unknown == ["beta"]


def test_validate_scope_propagates_qmd_daemon_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A down daemon reaches the caller as ``QmdDaemonUnavailable``."""
    from lies.qmd.access import QmdDaemonUnavailable

    async def _down(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        raise QmdDaemonUnavailable("daemon is down")

    monkeypatch.setattr(access, "daemon_tool", _down)

    with pytest.raises(QmdDaemonUnavailable):
        asyncio.run(access.validate_scope(["alpha"]))


def test_validate_scope_propagates_qmd_daemon_wedged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged daemon reaches the caller as ``QmdDaemonWedged``."""
    from lies.qmd.access import QmdDaemonWedged

    async def _wedge(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        raise QmdDaemonWedged("daemon wedged", last_output="")

    monkeypatch.setattr(access, "daemon_tool", _wedge)

    with pytest.raises(QmdDaemonWedged):
        asyncio.run(access.validate_scope(["alpha"]))


# --- the read-library-bodies hoisted bridge (I-9) ----------------------
#
# ``read_library_bodies`` opens one MCP session and issues one
# ``get`` per path under it. The tests below pin the tolerance
# boundary: a *passthrough* failure becomes ``None`` in the
# output, and every transport failure routes through the taxonomy
# and raises. The boundary is the point — a batched read that
# reports a slow daemon as an absent document tells the caller
# the corpus has nothing when the process is at fault. The FastMCP
# wire cannot be exercised in a unit test; the tests target
# ``read_library_bodies`` directly.


def test_read_library_bodies_returns_empty_list_for_empty_input() -> None:
    """No paths in, no daemon call, no session."""
    assert asyncio.run(access.read_library_bodies([])) == []


def test_read_library_bodies_per_path_failure_yields_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-path exception becomes ``None`` in the result list.

    The previous per-path behaviour caught ``Exception`` and
    logged + skipped; the hoisted bridge preserves that for
    individual paths while still hoisting the session. A
    document qmd cannot resolve must not kill the batch.
    """
    calls: list[str] = []

    async def _call_tool(name: str, arguments: dict[str, Any], **kw: Any) -> Any:
        calls.append(arguments.get("file", "?"))
        if "missing" in arguments["file"]:
            raise RuntimeError("Document not found")
        return SimpleNamespace(content=[], is_error=False)

    class _StubClient:
        async def __aenter__(self) -> "_StubClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

        async def call_tool(self, name: str, arguments: dict[str, Any], **kw: Any) -> Any:
            return await _call_tool(name, arguments, **kw)

    monkeypatch.setattr(access, "_daemon_client", lambda url: _StubClient())
    monkeypatch.setattr(access, "qmd_daemon_reachable", lambda url, timeout: True)

    results = asyncio.run(access.read_library_bodies(["alpha/ok.md", "alpha/missing.md"]))
    assert calls == ["alpha/ok.md", "alpha/missing.md"], (
        "one get per path, single session — the I-9 hoist"
    )
    assert results[0] is not None, "the ok path survives"
    assert results[1] is None, "the missing path is a None, not a session kill"


def test_read_library_bodies_a_transport_error_raises_not_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read timeout on path 2 raises; it is never a ``None`` entry.

    The defect this pins: ``read_library_bodies`` used to call
    ``client.call_tool`` directly, with the taxonomy living only in
    ``daemon_tool``. Every transport failure therefore became a
    per-path ``None``, and ``read.py`` turned that into a
    ``_missing`` entry — "the daemon could not resolve this path",
    a claim about the corpus that was really a claim about the
    process. A ``ReadTimeout`` was indistinguishable from a document
    that is genuinely absent.

    The stub raises the class fastmcp 4 actually raises for a slow
    daemon. ``httpx2.ReadTimeout`` is outside every ``httpx``-typed
    check, which is why the classifier matches on names.
    """
    httpx2 = pytest.importorskip("httpx2")

    class _StubClient:
        async def __aenter__(self) -> "_StubClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

        async def call_tool(self, name: str, arguments: dict[str, Any], **kw: Any) -> Any:
            if "b.md" in arguments["file"]:
                raise httpx2.ReadTimeout("slow daemon")
            return SimpleNamespace(content=[], is_error=False)

    monkeypatch.setattr(access, "_daemon_client", lambda url: _StubClient())
    monkeypatch.setattr(access, "qmd_daemon_reachable", lambda url, timeout: True)
    monkeypatch.setattr(access, "_daemon_log_tail", lambda: "expanding query 2/5")

    async def _recycle(**_kwargs: Any) -> Any:
        from lies.qmd.daemon import QmdState

        return QmdState(installed=True, running=True, pid=4242, detail="recycled")

    monkeypatch.setattr(access, "recycle_qmd_daemon", _recycle)

    with pytest.raises(access.QmdDaemonWedged) as excinfo:
        asyncio.run(access.read_library_bodies(["alpha/a.md", "alpha/b.md"]))
    assert excinfo.value.last_output == "expanding query 2/5"


def test_read_library_bodies_a_passthrough_failure_stays_per_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the taxonomy's passthrough class becomes a ``None``.

    The complement to the test above: after the taxonomy owns the
    transport classes, what is left reaching the per-path handler is
    a request the daemon rejected. That is genuinely per-path, so
    siblings survive it.
    """

    class _StubClient:
        async def __aenter__(self) -> "_StubClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

        async def call_tool(self, name: str, arguments: dict[str, Any], **kw: Any) -> Any:
            if "b.md" in arguments["file"]:
                raise ValueError("illegal header value")
            return SimpleNamespace(content=[], is_error=False)

    monkeypatch.setattr(access, "_daemon_client", lambda url: _StubClient())
    monkeypatch.setattr(access, "qmd_daemon_reachable", lambda url, timeout: True)

    results = asyncio.run(access.read_library_bodies(["alpha/a.md", "alpha/b.md"]))
    assert results[0] is not None
    assert results[1] is None


# --- the taxonomy matches what fastmcp actually raises -----------------
#
# The three cases below are transcribed from a probe run against the
# installed fastmcp 4.0.3 — an in-process `FastMCP` server over HTTP
# with a tool that sleeps past the client's read timeout, plus a closed
# port and a socket that accepts and never answers — not from its
# documentation. They are the whole reason `classify_call_error` checks
# exception class *names* rather than importing a specific httpx:
# fastmcp vendors its own httpx as `httpx2`, and `httpx2.ReadTimeout` is
# not a subclass of `httpx.ReadTimeout`. A taxonomy written against the
# `httpx` LIES declares matches none of them, and every wedge silently
# becomes a passthrough — the recycle never runs and the operator is
# told the daemon rejected the request.
#
# Re-derive by probe before changing the matcher; the installed
# fastmcp's answer is a property of that version, not of the protocol.


def test_a_vendored_httpx_read_timeout_is_still_a_wedge() -> None:
    httpx2 = pytest.importorskip("httpx2")
    assert access.classify_call_error(httpx2.ReadTimeout("")) == ("recycle-raise", False)


def test_a_vendored_httpx_connect_error_is_still_a_transport_error() -> None:
    httpx2 = pytest.importorskip("httpx2")
    assert access.classify_call_error(httpx2.ConnectError("")) == ("recycle-retry", True)


def test_a_dead_session_wrapped_by_fastmcp_is_classified_through_its_cause() -> None:
    # What fastmcp 4.0.3 raises for a daemon that is not listening.
    httpx2 = pytest.importorskip("httpx2")
    wrapped = RuntimeError("Client failed to connect: All connection attempts failed")
    wrapped.__cause__ = httpx2.ConnectError("refused")

    assert access.classify_call_error(wrapped) == ("recycle-retry", True)


def test_a_wedge_wrapped_by_fastmcp_is_a_wedge_not_a_passthrough() -> None:
    httpx2 = pytest.importorskip("httpx2")
    wrapped = RuntimeError("Client failed to connect: ")
    wrapped.__cause__ = httpx2.ReadTimeout("")

    assert access.classify_call_error(wrapped) == ("recycle-raise", False)


def test_a_dead_mcp_session_is_a_wedge() -> None:
    # mcp.types.CONNECTION_CLOSED: the dispatcher saw the socket go
    # before the client saw a timeout, and reported it through the
    # protocol layer instead.
    err = mcp.MCPError(code=-32000, message="Connection closed")
    assert access.classify_call_error(err) == ("recycle-raise", False)


def test_a_cause_chain_cannot_loop_forever() -> None:
    # A wrapper that names itself as its own cause would hang the
    # classification; the chain walk has to be bounded by identity.
    err = RuntimeError("Client failed to connect: ")
    err.__cause__ = err

    assert access.classify_call_error(err) == ("passthrough", False)


def test_an_httpx_client_factory_nobody_can_call_is_not_a_seam() -> None:
    # fastmcp invokes the httpx factory with `follow_redirects=`, which
    # the shipped factory did not accept — so *every* HTTP daemon call,
    # agent path included, failed at connect with a TypeError before
    # reaching any of the taxonomy above.
    import inspect

    from lies.qmd.mcp import _build_qmd_httpx_client

    params = inspect.signature(_build_qmd_httpx_client).parameters
    assert "follow_redirects" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    ), "fastmcp passes follow_redirects=; the factory must accept it"


# --- what `data_dir` actually controls ----------------------------------
#
# `ensure_qmd_daemon(data_dir=...)` reads like it selects the index the
# daemon serves, and `recycle_qmd_daemon(data_dir=...)` likewise. Neither
# is true, and the reason matters for anyone tempted to "fix" the two
# call sites to agree: `_spawn_qmd_daemon` runs `qmd mcp --http --daemon`
# with `cwd=Path.cwd()` and never reads the argument, so `data_dir` only
# ever reaches `write_sidecar_data_dir`. The daemon that comes up is the
# same whichever value you pass.


def test_the_spawned_daemon_never_receives_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_spawn_qmd_daemon`` ignores any data dir; the spawn is cwd-only.

    A source-level pin rather than a behavioural one, because the claim
    is about what is *not* passed. If someone later threads ``data_dir``
    into the spawn, this fails — which is the point at which the
    ``_recycle_data_dir`` vs ``ensure_qmd_daemon`` question becomes a
    real one rather than a bookkeeping detail.
    """
    from lies.qmd import daemon as qmd_daemon

    source = Path(qmd_daemon.__file__).read_text(encoding="utf-8")
    start = source.index("def _spawn_qmd_daemon(")
    end = source.index("def ", start + 10)
    body = source[start:end]

    assert "_spawn_qmd_daemon(" in body
    assert "data_dir" not in body, (
        "_spawn_qmd_daemon now reads a data_dir; the daemon it starts may "
        "differ from the one a recycle starts, and the two call sites' "
        "disagreement is no longer bookkeeping"
    )
    assert "cwd=" in body, "the spawn is cwd-based; that is what makes data_dir inert"


# Removed under M-7: a tautology that monkeypatched
# ``write_sidecar_data_dir`` (a function ``_recycle_data_dir`` never
# calls) and asserted the result was a Path. The source-level pin
# above is the load-bearing test for the data-dir flow.
