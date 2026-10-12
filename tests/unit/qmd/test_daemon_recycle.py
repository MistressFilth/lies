"""The recycle readiness probe: what it retries on, and what it costs."""

from __future__ import annotations

import re
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lies.qmd import daemon as qmd_daemon

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS = REPO_ROOT / "AGENTS.md"

# --- the shape a stopped daemon actually produces -----------------------
#
# A stopped qmd daemon raises a bare ``RuntimeError`` whose cause is
# fastmcp's *vendored* ``httpx2``, a different module from the httpx LIES
# imports — so neither ``httpx.HTTPError`` nor ``OSError`` matches. Only
# the wrapper is catchable without naming a vendored module. See
# AGENTS.md for the full chain.


_HTTPX2_MISSING = (
    "fastmcp's vendored httpx is not importable as 'httpx2'. That is the "
    "signal to RE-DERIVE _RECYCLE_PROBE_ERRORS against the installed "
    "fastmcp: probe a closed port and record the new exception class. "
    "Skip, not fail — a re-vendored httpx is not this suite's defect."
)


def _measured_dead_session_error() -> RuntimeError:
    """The exact exception ``fastmcp.Client(url).list_tools()`` raises."""
    httpx2 = pytest.importorskip("httpx2", reason=_HTTPX2_MISSING)

    try:
        raise httpx2.ConnectError("All connection attempts failed")
    except httpx2.ConnectError as cause:
        err = RuntimeError("Client failed to connect: All connection attempts failed")
        err.__cause__ = cause
        return err


class _DeadSessionClient:
    """A ``fastmcp.Client`` stand-in for a daemon that is not serving."""

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
    """Fail this file's tests if any of them opens a loopback socket."""
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
    """Give ``recycle_qmd_daemon`` a clock it drives, at zero wall cost."""
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
    """The measured dead-session shape is retried, not raised at the caller."""
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

    # A 30s budget at 0.5s per poll is 60 attempts. A floor rather than
    # an equality: the fake clock accumulates floats and wobbles by one
    # iteration at the boundary.
    assert len(_DeadSessionClient.attempts) >= 50
    # One sleep per probe — the trailing sleep is what advances the clock
    # past the deadline.
    assert clock["sleeps"] == len(_DeadSessionClient.attempts)
    assert excinfo.value.ready_timeout_s == 30.0
    assert "no listener" in excinfo.value.last_state.detail


def test_the_measured_exception_is_not_in_any_legacy_arm() -> None:
    """Why the legacy three arms could not have caught it."""
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
    """``RuntimeError`` is broad, and LIES' own failure is a ``RuntimeError``."""
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
        """The bare form only — see the 'does not enforce' list above."""
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
    """The happy path is untouched: one probe, no sleep, state returned."""
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
    """A booting daemon answering 404/503 arrives as ``MCPError``."""
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
    """The worst case is a measured floor and ceiling, with its traps intact."""
    doc = " ".join(_ceiling_doc().split())
    headline = _headline_bound(doc)

    assert headline is not None, (
        "the one-line bound summary is gone; every wrong answer this file "
        "records began by replacing it"
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
    assert "measured" in doc, (
        "these are measurements; calling them derived invites re-deriving them wrongly"
    )

    # The headline is a *summary of this table*, so its two numerals are
    # checked against the table's floor and ceiling.
    headline_figures = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?) ?s", headline)]
    assert len(headline_figures) == 2, (
        f"the headline must quote exactly two figures, a floor and a "
        f"ceiling: {headline!r} quotes {headline_figures}"
    )
    table = _doc_table_rows(_ceiling_doc())
    assert len(table) == len(EXPECTED_ROWS), (
        "the table this headline summarises is not the expected shape; fix "
        "that first or this comparison means nothing"
    )
    floor_row, ceiling_row = table[0][1], table[-1][1]
    floor_match = re.search(r"(\d+(?:\.\d+)?)", floor_row)
    ceiling_match = re.search(r"(\d+(?:\.\d+)?)", ceiling_row)
    assert floor_match and ceiling_match, (
        f"could not read a figure from the floor/ceiling rows: {floor_row!r} / {ceiling_row!r}"
    )
    assert headline_figures[0] == float(floor_match.group(1)), (
        f"the headline says {headline_figures[0]:g}s floor but the table's "
        f"floor row reads {floor_row!r}. They are the same measurement and "
        f"must not disagree."
    )
    assert headline_figures[1] == float(ceiling_match.group(1)), (
        f"the headline says {headline_figures[1]:g}s ceiling but the table's "
        f"ceiling row reads {ceiling_row!r}. A headline that contradicts "
        f"its own table is how '47 s' reached a report while the table "
        f"said 61."
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
        "the claim must record that the earlier ~80s ceiling was "
        "unreachable, not merely loose - that is why the shape changed"
    )
    assert "cliff" in doc, (
        "the hanging-status row must be named a cliff, not merely "
        "'unstable': it swings 14.5s on 0.25s of headroom, and calling "
        "that noise is what let a flat 46s be printed"
    )
    assert "_POLL_INTERVAL_S" in doc, (
        "the claim must name the one constant that is neither a "
        "timeout nor a sleep and therefore does not scale by itself"
    )
    assert "Scaled:" in doc and "does not scale by itself" in doc, (
        "what was scaled must be stated precisely, not implied by a blanket 'every constant'"
    )


#: Every name ``_measure`` is allowed to patch. An allowlist, so a new
#: patch target cannot be added without naming it here.
EXPECTED_PATCH_TARGETS: frozenset[str] = frozenset(
    {
        # Popen only: keeps ``_run_qmd``'s reader loop, bounds and
        # ``sel.select`` granularity on the production implementation.
        "_subprocess.subprocess",
        # The granularity this harness scales.
        "_subprocess._POLL_INTERVAL_S",
        # The wake counter, so a stubbed ``_run_qmd`` is caught at runtime
        # by the harness itself as well as by this file.
        "_subprocess.selectors.DefaultSelector",
        # The time constants, at 1/N.
        "d.STATUS_TIMEOUT_S",
        # The reap, neutered the way the original does it.
        "d._reap_qmd_daemon",
        "d.os.kill",
        "d._pid_alive",
        # Never touch the operator's sidecar.
        "d.write_sidecar_scope",
        # The probe, which must never serve.
        "d.fastmcp.Client",
    }
)


#: The rows the ceiling claim must carry, as ``(configuration, marker)``.
#: ``marker`` is the annotation the row must bear — ``""`` for a row that
#: carries only its figure.
#:
#: This is the structured constant the prose is checked *against*, so the
#: table can be deleted and the test still fails.
EXPECTED_ROWS: tuple[tuple[str, str], ...] = (
    ("prompt status, prompt spawn", "floor"),
    ("SIGTERM", ""),
    ("spawn hangs", ""),
    ("qmd status` hangs", "cliff"),
    ("both hang", "ceiling"),
)

#: Figures that must never be quoted as the **bound**. Both were
#: published answers a reader could copy, and both were wrong: 45 s
#: summed mutually exclusive component bounds, 80 s was not an upper
#: bound at all.
#:
#: Deliberately *not* checked against every row. 45 s is also a
#: legitimately measured row — the spawn-hang scenario reads 45 s — so a
#: blanket ban would forbid a true figure to police a false bound.
FORBIDDEN_BOUNDS = ("45 s", "80 s", "~45", "~80")

#: The row that must carry a RANGE rather than a point figure, and the
#: measured ends it must reach. Measured 44.9 ... 59.4 s by sweeping the
#: bound at 1/100 scale; the printed range must cover that, to within
#: half a second of rounding at either end.
#:
#: The direction of the test matters. The printed range must not claim a
#: floor *above* the measured minimum -- a figure the configuration never
#: produced -- and must not stop short of the measured maximum.
CLIFF_ROW = "qmd status hangs"
CLIFF_MEASURED_S = (44.9, 59.4)
CLIFF_ROUNDING_S = 0.5


def _ceiling_doc() -> str:
    """AGENTS.md, where the recycle-ceiling claim lives."""
    assert AGENTS.is_file(), f"AGENTS.md has moved: {AGENTS}"
    return AGENTS.read_text(encoding="utf-8")


def _doc_table_rows(doc: str) -> list[tuple[str, str]]:
    """Parse the markdown pipe table into ``(left, right)`` rows."""
    rows: list[tuple[str, str]] = []
    seen_rule = False
    for line in doc.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2 or not cells[0]:
            continue
        if set(cells[0]) <= {"-", ":", " "}:
            # The ``|---|---|`` alignment rule; data rows follow it.
            seen_rule = True
            continue
        if not seen_rule:
            # The header row, above the rule.
            continue
        rows.append((cells[0], cells[1]))
    return rows


def _headline_bound(doc: str) -> str | None:
    """The one-line bound summary, flattened, or ``None``."""
    flat = " ".join(doc.split())
    marker = "Worst case:"
    if marker not in flat:
        return None
    tail = flat.split(marker, 1)[1]
    end = tail.find(". ")
    return (marker + (tail[:end] if end != -1 else tail)).strip()


def test_the_ceiling_is_never_a_single_figure() -> None:
    """The figures are a table of configurations, and the table is load-bearing."""
    rows = _doc_table_rows(_ceiling_doc())

    assert rows, (
        "the ceiling table is gone. Its rows are what carry each "
        "configuration's figure; without it the claim is back to a "
        "single number, which is the failure this test exists to prevent."
    )
    assert len(rows) == len(EXPECTED_ROWS), (
        f"expected {len(EXPECTED_ROWS)} configuration rows, found {len(rows)}: "
        f"{[r[0] for r in rows]}. A table that collapses to fewer rows is "
        f"the exact shape both wrong single-figure answers had."
    )
    figures: list[float] = []
    ranges: dict[str, tuple[float, float]] = {}

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

        # A row may carry a point figure ("61 s") or a range ("45-59 s").
        # The range form is required for :data:`CLIFF_ROW`; accepted
        # elsewhere so a future row that is genuinely a band does not
        # have to be special-cased twice.
        span = re.search(r"\*{0,2}(\d+(?:\.\d+)?)\s*[-\u2013]\s*(\d+(?:\.\d+)?) ?s", got_right)
        point = re.search(r"\*{0,2}(\d+(?:\.\d+)?) ?s", got_right)
        assert span or point, f"row {flat_cfg!r} does not carry a figure in seconds: {got_right!r}"
        if span:
            lo, hi = float(span.group(1)), float(span.group(2))
            assert lo < hi, f"row {flat_cfg!r} reads a backwards range: {got_right!r}"
            ranges[want_cfg] = (lo, hi)
            figures.extend((lo, hi))
        else:
            figures.append(float(point.group(1)))  # type: ignore[union-attr]

        # The cliff row must carry a range, and it must span the measured
        # band. A point figure here is the exact defect this round
        # corrects: 46 s is right on the harness's side of the boundary
        # and wrong by 14 s on the other.
        if want_cfg == CLIFF_ROW:
            assert span, (
                f"row {flat_cfg!r} must print a range, not a point: "
                f"{got_right!r}. The configuration swings "
                f"{CLIFF_MEASURED_S[0]:g}...{CLIFF_MEASURED_S[1]:g}s because "
                f"the budget admits a second in-loop status call only while "
                f"2 x bound + poll < budget. A single figure is right by "
                f"coincidence and wrong by up to 14 s."
            )
            lo, hi = ranges[want_cfg]
            tol = CLIFF_ROUNDING_S
            assert lo <= CLIFF_MEASURED_S[0] + tol, (
                f"row {flat_cfg!r} starts at {lo:g}s, above the measured "
                f"minimum of {CLIFF_MEASURED_S[0]:g}s (+{tol:g} rounding). "
                f"That figure was never produced by this configuration."
            )
            assert hi >= CLIFF_MEASURED_S[1] - tol, (
                f"row {flat_cfg!r} stops at {hi:g}s, short of the measured "
                f"maximum of {CLIFF_MEASURED_S[1]:g}s (-{tol:g} rounding). "
                f"A reader copying that figure is understated by "
                f"{CLIFF_MEASURED_S[1] - hi:.0f} s on the far side of the "
                f"boundary."
            )

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
    """The band row is justified by arithmetic, not by a remembered spread."""
    doc = " ".join(_ceiling_doc().split())

    assert "0.1475" in doc, (
        "the claim must quote the measured sweep boundary (0.1475) at "
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
    # The far end, and the sweep that produced it. Both ends of a band
    # have to be stated, or a reader has a range with no evidence behind
    # its upper half.
    assert "44.9 to 59.4" in doc, (
        "the claim must state BOTH ends of the cliff in the prose. A "
        "version that gave the boundary and the near side and omitted the "
        "far one made the printed 46 s look like the whole range. Checked "
        "as the phrase rather than as a bare '59.4', because the sweep "
        "table carries it too."
    )
    assert "0.1450" in doc and "0.1470" in doc, (
        "the sweep that derives the boundary must stay. It is the only "
        "evidence for the 59.4 s end, and it brackets the step: 0.1450 "
        "takes two in-loop calls, 0.1470 takes one."
    )
    # "cliff", not "unstable". Asserted on the *sentence*, not the whole
    # text, because the table's row marker also carries the word.
    assert re.search(r"row is a cliff", doc), (
        "the prose must call the band a cliff. It is not noise: 0.25 s of "
        "headroom buys a whole STATUS_TIMEOUT_S, and 'unstable' invites a "
        "reader to average it away."
    )
    assert not re.search(r"row is merely unstable", doc), (
        "describing a 14.5 s swing as unstable is the mislabelling this "
        "replaced; it reads as noise rather than as a boundary"
    )


def test_the_ceiling_harness_still_pins_the_poll() -> None:
    """The poll pin exists, and the harness it pins is the real reader loop."""
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
        and isinstance(node.args[0], (ast.Name, ast.Attribute))
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    }
    targets = sorted(f"{obj}.{attr}" for obj, attr in patched)

    # An ALLOWLIST, so this fails closed: every member of `targets` is
    # built as f"{obj}.{attr}" and therefore always contains a dot, so a
    # substring blacklist could never fire.
    unexpected = sorted(set(targets) - EXPECTED_PATCH_TARGETS)
    assert not unexpected, (
        f"_measure patches {unexpected}, which is not in "
        f"EXPECTED_PATCH_TARGETS. Patching `d._run_qmd` deletes the reader "
        f"loop, and _POLL_INTERVAL_S lives inside it -- so the harness's "
        f"poll rescaling becomes inert and every figure it reports is "
        f"quietly wrong. Patch `_subprocess.subprocess` (i.e. Popen) "
        f"instead and the reader loop, its bounds and its select() "
        f"granularity stay on the production implementation."
    )
    missing = sorted(EXPECTED_PATCH_TARGETS - set(targets))
    assert not missing, (
        f"_measure no longer patches {missing}. If one of these was "
        f"removed deliberately, remove it from EXPECTED_PATCH_TARGETS too "
        f"-- leaving it here makes this test a wall rather than a record."
    )
    assert hasattr(mod, "_POLL_INTERVAL_S"), (
        "the harness must re-export the constant it scales, so a reader can "
        "check the scaling without reading the patch line"
    )
    assert mod._POLL_INTERVAL_S == pytest.approx(0.1)

    # The harness's own runtime guard must still exist. `_measure`
    # asserts on the `sel.select` wake count, which is what turns a
    # stubbed `_run_qmd` into a named assertion instead of an
    # `AttributeError` from inside `qmd_daemon_state`.
    #
    # Checking the assertion is *present* is guard-removal territory and
    # cannot be defended from inside the guard itself. It is here so that
    # deleting it is a red run rather than a silent one -- the same
    # contract as the check that the poll test still exists.
    asserted_names: set[str] = set()
    for node in ast.walk(measure):
        if isinstance(node, ast.Assert):
            asserted_names |= {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
    assert "_SELECT_WAKES" in asserted_names, (
        "_measure no longer asserts on the sel.select wake count. Without "
        "it a stubbed _run_qmd surfaces as an AttributeError from inside "
        "qmd_daemon_state rather than as a named assertion about the "
        "thing that actually changed."
    )


async def test_the_recycle_spawns_on_the_port_it_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The restart lands on the port the readiness probe is watching."""
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
