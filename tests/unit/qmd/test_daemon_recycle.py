"""The recycle readiness probe: what it retries on, and what it costs.

Every test here is stub-only and sleeps zero real seconds — the
``asyncio.sleep`` between attempts is replaced by a counter-driven no-op
and ``ready_timeout`` is set in the milliseconds. Spawning a real qmd
daemon is not this file's job; see
``tests/integration/test_qmd_spawn_throwaway.py``.
"""

from __future__ import annotations

import re
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

    **It observes, it does not block.** A connection that gets through is
    still made; the test is failed afterwards. That is deliberate: a
    guard that raised mid-connection would leave the test's own state
    inconsistent, and the thing being guarded is a test-suite accident,
    not production traffic. Two known gaps, so nobody reads this as a
    firewall: ``connect_ex`` is not intercepted, and a connect performed
    in a thread that outlives the fixture would escape it. Neither
    occurs in this file, and both would be caught by
    ``tools/qmd_unit_network_audit.py``.

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


def test_the_docstring_states_a_measured_floor_and_ceiling() -> None:
    """The worst case is a measured floor and ceiling, with its traps intact.

    **Structure, not decimals.** Independent harnesses measure this path
    and disagree at the 1-4 s level, because they differ in what they
    treat as scaled. Asserting a literal like ``"31.0 s"`` pins one
    harness's arithmetic as though it were a property of the code, and
    makes a *more* accurate measurement look like a regression. So the
    decimals live in the committed harness
    (``tests/integration/test_qmd_recycle_ceiling.py``), which re-derives
    them and asserts the shape. This test asserts the shape is still
    *described*.

    Three wrong answers have lived on this line - "45 s" (mutually
    exclusive bounds), "~80 s" (unreachable), and "+up to 4 s" attached
    to a row that cannot satisfy it - so the trap each one fell into is
    pinned as prose.

    **The headline sentence is parsed, not searched.** Asserting that
    "floor" and "ceiling" appear *somewhere* is what let M38 through:
    reverting the headline to "a ~80 s ceiling" left both words present
    in the table's row markers, so the test passed on a docstring whose
    one-line summary was the answer that had already been retracted.
    """
    doc = " ".join((qmd_daemon.recycle_qmd_daemon.__doc__ or "").split())
    headline = _headline_bound(doc)

    assert headline is not None, (
        "the docstring's one-line bound summary is gone; every wrong answer "
        "this file records began by replacing it"
    )
    assert re.search(r"\bfloor\b", headline) and re.search(r"\bceiling\b", headline), (
        f"the headline must frame the bound as a range with named ends: {headline!r}"
    )
    for bad in FORBIDDEN_BOUNDS:
        assert bad not in headline, (
            f"the headline quotes {bad!r} as the bound: {headline!r}. 45 s "
            f"summed mutually exclusive component bounds and 80 s was not "
            f"an upper bound at all."
        )
    assert "measured, not" in doc, (
        "these are measurements; calling them derived invites re-deriving them wrongly"
    )
    assert "spawn has its own" in doc, (
        "the spawn's own 15s bound must be named; it is the biggest "
        "single contributor and no single-figure summary ever had it"
    )
    assert "reachable in exactly one row" in doc, (
        "the grace windows are reachable in one configuration only; a "
        "hanging qmd status reports running=False and forgoes them"
    )
    assert "unreachable" in doc, (
        "the docstring must record that the earlier ~80s ceiling was "
        "unreachable, not merely loose - that is why the shape changed"
    )
    assert "unstable" in doc, (
        "the hanging-status row is genuinely unstable; presenting it as a "
        "fixed figure is what produced the +5.3s anomaly"
    )
    assert "_POLL_INTERVAL_S" in doc, (
        "the docstring must name the one constant that is neither a "
        "timeout nor a sleep and therefore does not scale by itself"
    )
    assert "Scaled:" in doc and "does not scale by itself" in doc, (
        "what was scaled must be stated precisely, not implied by a blanket 'every constant'"
    )


#: The rows ``recycle_qmd_daemon``'s docstring must carry, as
#: ``(configuration, marker)``. ``marker`` is the annotation the row must
#: bear — ``""`` for a row that carries only its figure.
#:
#: This is the structured constant the prose is checked *against*, so the
#: table can be deleted and the test still fails. The predecessor's
#: version was ten substring checks on the flattened docstring, and
#: collapsing every row to one ``**~47 s**`` figure passed it — so did
#: deleting the table outright, because ``assert "configuration" in doc``
#: was satisfied by the word appearing in an unrelated sentence.
EXPECTED_ROWS: tuple[tuple[str, str], ...] = (
    ("daemon down", "floor"),
    ("SIGTERM", ""),
    ("spawn hangs", ""),
    ("qmd status hangs", "unstable"),
    ("status and spawn both hang", "ceiling"),
)

#: Figures that must never be quoted as the **bound**. Both were
#: published answers a reader could copy, and both were wrong: 45 s
#: summed mutually exclusive component bounds, 80 s was not an upper
#: bound at all.
#:
#: Deliberately *not* checked against every row. 45 s is also a
#: legitimately measured row — the spawn-hang scenario reads 45 s — so a
#: blanket ban would forbid a true figure to police a false bound. The
#: check below applies these to the rows that make a bound claim, which
#: is the only place they were ever wrong.
FORBIDDEN_BOUNDS = ("45 s", "80 s", "~45", "~80")


def _doc_table_rows(doc: str) -> list[tuple[str, str]]:
    """Parse the docstring's RST simple table into ``(left, right)`` rows.

    Returns ``[]`` when there is no table, which every caller below must
    treat as a failure — that is the point. Parsed rather than matched,
    because the defect being pinned is a table that collapses or
    disappears, and no substring check can tell those apart from prose
    that happens to share a word.

    Cells are split at the **rule line's column boundary**, not at the
    first run of whitespace. Splitting on whitespace is what a first
    attempt did, and it silently mis-parsed the widest configuration —
    whose label butts against its own figure with a single space — into
    one cell and reported it as a row with no figure in it.
    """
    lines = doc.splitlines()
    rules = [i for i, ln in enumerate(lines) if _is_rule(ln)]
    if len(rules) < 3:
        return []
    boundary = len(lines[rules[1]].strip().split("  ")[0])
    rows = []
    for ln in lines[rules[1] + 1 : rules[2]]:
        if not ln.strip():
            continue
        stripped = ln.rstrip()
        left = stripped[:boundary].strip()
        right = stripped[boundary:].strip()
        if not left:
            # The label overflowed its column: take the break between the
            # two cells so a too-long configuration is still read as a
            # label plus a figure rather than as a figure-less row.
            head, _, tail = stripped.partition("**")
            left, right = head.strip(), "**" + tail.strip()
        rows.append((left, right))
    return rows


def _is_rule(line: str) -> bool:
    """True for an RST simple-table rule (``====  ====``)."""
    stripped = line.strip()
    if not stripped or "=" not in stripped:
        return False
    return all(part and set(part) == {"="} for part in stripped.split("  ") if part)


def _headline_bound(doc: str) -> str | None:
    """The docstring's one-line bound summary, flattened, or ``None``.

    Located by its opening phrase rather than by a regex over the whole
    docstring, because "floor" and "ceiling" also appear in the table's
    row markers and in the prose that records their history. Only the
    sentence a reader reads first makes a bound *claim*, and only that
    one is what every retracted answer here replaced.
    """
    flat = " ".join(doc.split())
    marker = "Worst case:"
    if marker not in flat:
        return None
    tail = flat.split(marker, 1)[1]
    end = tail.find(". ")
    return (marker + (tail[:end] if end != -1 else tail)).strip()


def test_the_ceiling_is_never_a_single_figure() -> None:
    """The figures are a table of configurations, and the table is load-bearing.

    Three wrong answers have lived on this line — "45 s" (mutually
    exclusive bounds), "~80 s" (unreachable), "+up to 4 s" on a row that
    cannot satisfy it — and each was a *single* number. The structural
    claim is the one that survives all of them: one number cannot be
    right for five configurations, so the docstring must still carry a
    row per configuration.

    Asserted against :data:`EXPECTED_ROWS` rather than by searching the
    flattened prose. The previous version of this test was ten substring
    checks, and both of the reviewer's probes passed it: collapsing the
    whole table to one figure, and deleting the table entirely.
    """
    doc = qmd_daemon.recycle_qmd_daemon.__doc__ or ""
    rows = _doc_table_rows(doc)

    assert rows, (
        "the ceiling table is gone. Its rows are what carry each "
        "configuration's figure; without it the docstring is back to a "
        "single number, which is the failure this test exists to prevent."
    )
    assert len(rows) == len(EXPECTED_ROWS), (
        f"expected {len(EXPECTED_ROWS)} configuration rows, found {len(rows)}: "
        f"{[r[0] for r in rows]}. A table that collapses to fewer rows is "
        f"the exact shape both wrong single-figure answers had."
    )
    figures: list[float] = []

    for (want_cfg, want_marker), (got_cfg, got_right) in zip(EXPECTED_ROWS, rows):
        flat_cfg = " ".join(got_cfg.split())
        assert want_cfg in flat_cfg, (
            f"row {want_cfg!r} is missing or renamed: the table now reads "
            f"{flat_cfg!r}. Each configuration needs its own row because no "
            f"single figure is right for all of them."
        )
        assert want_cfg not in got_right, (
            f"row {want_cfg!r} lost its figure; the configuration column "
            f"swapped with the measurement column"
        )
        if want_marker:
            assert want_marker in flat_cfg + " " + got_right, (
                f"row {want_cfg!r} must carry the {want_marker!r} marker: it "
                f"reads {flat_cfg!r} | {got_right!r}"
            )
        # Every row carries a figure in seconds. Checked per row, so a row
        # stripped of its figure cannot hide behind a neighbour's.
        assert got_right, f"row {flat_cfg!r} has no measured figure in it"
        match = re.search(r"\*{0,2}(\d+(?:\.\d+)?) ?s", got_right)
        assert match, f"row {flat_cfg!r} does not carry a figure in seconds: {got_right!r}"
        figures.append(float(match.group(1)))

        # The two rows that *make a bound claim* — floor and ceiling —
        # must not quote one of the published-wrong answers. A middle row
        # may legitimately read 45 s; the ceiling may not be 45 s.
        if want_marker in ("floor", "ceiling"):
            for bad in FORBIDDEN_BOUNDS:
                assert bad not in got_right, (
                    f"the {want_marker} is quoted as {bad!r}: {got_right!r}. "
                    f"Both 45 s and 80 s were published answers that were "
                    f"wrong — one summed mutually exclusive bounds, the "
                    f"other was not an upper bound at all."
                )

    assert min(figures) == figures[0], (
        f"the row marked (floor) reads {figures[0]:g} s but another row "
        f"reads less ({figures}); a floor that is not the floor is worse "
        f"than no floor"
    )
    assert max(figures) == figures[-1], (
        f"the row marked (ceiling) reads {figures[-1]:g} s but another row "
        f"reads more ({figures}); a ceiling under the measured maximum "
        f"understates the worst case"
    )


def test_the_unstable_row_says_why_and_names_its_cause() -> None:
    """The band row is justified by arithmetic, not by a remembered spread.

    An earlier version claimed the row was unstable because it had been
    observed at 17.7 s and 19.7 s. Counting the ``qmd status`` calls
    across 20 runs of the old harness: 3 in 20 of 20. The spread was
    ``time.sleep`` jitter inside a stubbed ``_run_qmd``; the knife-edge
    was real but was not what had been observed.
    """
    doc = " ".join((qmd_daemon.recycle_qmd_daemon.__doc__ or "").split())

    assert "0.1475" in doc, (
        "the docstring must quote the measured sweep boundary (0.1475) at "
        "which a second in-loop qmd status call stops fitting. The band is "
        "only defensible with the arithmetic that makes it a band."
    )
    assert "0.25 s of headroom" in doc, (
        "state how far the harness's own status bound clears that boundary; "
        "it is the difference between a band and a knife-edge that happens "
        "not to fire"
    )
    assert "3 in 20 runs" in doc, (
        "the correction to the earlier instability claim must stay recorded: "
        "the call count never varied, so the old spread was stub jitter"
    )


def test_the_ceiling_harness_still_pins_the_poll() -> None:
    """The poll pin exists, and the harness it pins is the real reader loop.

    Round 3 deleted a test by rewriting two others by line-slicing; the
    suite stayed green at 2036 instead of 2037, and the port pin went
    with it. Nothing in a test suite notices its own missing tests, so
    this one does, from the *other* file.

    Two claims, both structural:

    1. ``test_the_poll_granularity_is_reached_and_scaled`` is defined in
       the harness. M34 and M34b weaken or delete it; a suite-wide green
       is then reporting on a suite with one fewer guard, which is how
       the round-3 false zero survived three rounds of review.

    2. ``_measure`` never patches ``daemon._run_qmd``. That is the edit
       that made round 3's ``_POLL_INTERVAL_S`` patch inert: the constant
       lives inside ``_run_qmd``'s reader loop, so a harness that
       replaces ``_run_qmd`` never reads it. The harness must patch
       ``subprocess.Popen`` instead, leaving the reader loop real.

    Neither claim reads the harness's *assertions* — reading a test's own
    source to check its logic is the tautology this file's predecessor
    shipped. These check that the guard is present and aimed correctly;
    whether it has teeth is M32/M33's job, and both are caught.
    """
    import importlib.util

    harness = Path(__file__).resolve().parents[2] / "integration" / "test_qmd_recycle_ceiling.py"
    assert harness.is_file(), f"the ceiling harness has moved: {harness}"

    spec = importlib.util.spec_from_file_location("_lies_ceiling_harness", harness)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert hasattr(mod, "test_the_poll_granularity_is_reached_and_scaled"), (
        "the harness's poll pin is gone. Nothing in a test suite notices "
        "its own missing tests — measured: deleting it left every test "
        "green, which is how round 3 shipped a pin that could not fail."
    )

    import ast

    def dotted_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{dotted_name(node.value)}.{node.attr}"
        return ast.unparse(node)

    measure = next(
        n
        for n in ast.walk(ast.parse(harness.read_text(encoding="utf-8")))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_measure"
    )
    patched = {
        (dotted_name(node.args[0]), node.args[1].value)
        for node in ast.walk(measure)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "object"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    }
    targets = sorted(f"{obj}.{attr}" for obj, attr in patched)
    assert "_run_qmd" not in targets, (
        f"_measure patches {targets}. Patching _run_qmd deletes the reader "
        f"loop, and _POLL_INTERVAL_S lives inside it — so the harness's "
        f"poll rescaling becomes inert and every figure it reports is "
        f"quietly wrong."
    )
    assert "_subprocess.subprocess" in targets, (
        f"_measure must patch _subprocess.subprocess (i.e. Popen) instead; "
        f"it patches {targets}. Patching Popen keeps _run_qmd's reader "
        f"loop, its bounds and its select() granularity on the production "
        f"implementation."
    )
    assert hasattr(mod, "_POLL_INTERVAL_S"), (
        "the harness must re-export the constant it scales, so a reader can "
        "check the scaling without reading the patch line"
    )
    assert mod._POLL_INTERVAL_S == pytest.approx(0.1)


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
