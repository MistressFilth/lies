"""Measure the recycle's worst case, and assert only its STRUCTURE.

Every time constant in the path is scaled by ``1/N`` so a real exhaustion
finishes in seconds, then the measured wall time is scaled back. N = 100
puts the floor at ~0.31 s and the ceiling at ~0.61 s.

**What is scaled.** ``STATUS_TIMEOUT_S``, ``ready_timeout``,
``poll_interval``, the reap's ``grace``/``poll``, **and**
``_subprocess._POLL_INTERVAL_S``. The last one is neither a timeout nor a
sleep — it is the granularity of the reader loop's ``sel.select()`` — so
it does not scale by itself, and an unscaled 0.1 s survives the rescaling
and lands as up to ~10 s per simulated hanging ``qmd status`` call in
real-scale terms.

**The reader loop is real. That is the whole reason the poll matters.**
An earlier version of this harness replaced ``daemon._run_qmd`` outright,
which meant ``_POLL_INTERVAL_S`` was never read and patching it was
inert — six patched runs and three unpatched runs produced overlapping
distributions. It also produced a *tautological pin*: the test that
claimed to check the harness scaled the constant searched its own source
for a string that sat in the assertion's own left operand, so deleting
the patch left every test green.

So ``_run_qmd`` is no longer stubbed. It runs, over real pipes, with
``subprocess.Popen`` swapped for a child that emits exactly the bytes the
scenario calls for and then either closes (prompt) or stays silent
(``hangs``). The ``sel.select()`` loop, its bounds, its ``read1`` and its
EOF handling are all the production ones. Measured both ways, five runs
each:

=========================  ==========  ==========  ===========
scenario                   stub+patch  stub-patch  real, no patch
=========================  ==========  ==========  ===========
daemon down                30-31 s     30-31 s     30 s
status "running", no kill  34-35 s     34-35 s     34-35 s
daemon down, spawn hangs   31 s        30-31 s     50-52 s
``qmd status`` hangs       47-50 s     46-49 s     64-66 s
status and spawn hang      48-49 s     48-49 s     **86-88 s**
=========================  ==========  ==========  ===========

The first two columns are identical, which is the inert patch made
visible: with ``_run_qmd`` stubbed, patching the constant changes
nothing. The last column is 25 s above the band, so removing the patch
from *this* harness is a mutation the ceiling test catches.

**The axis that matters is what ``qmd status`` reports, not "is it
prompt".** That single fact decides three things at once:

- whether ``_reap_qmd_daemon`` spends its two 2 s grace windows (only
  when the status says *running* with a live pid);
- whether the reap kills anything;
- how long each of its four ``qmd status`` calls costs.

So the scenarios below vary what status *reports*, not merely how fast
it answers. Every row is a configuration the code can actually be in,
and every delta matches its own stated cap.

**Why a band and not exact figures.** Independent harnesses measure this
path and disagree at the 1-4 s level, because they differ in what they
treat as scaled. The decimals belong to a harness, not to the code. What
belongs to the code is the shape: a floor, a ceiling, the spawn's own
bound dominating the middle, and grace windows spent only where they can
be. Those are asserted; the figures are reported.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from lies.qmd import _subprocess
from lies.qmd import daemon as d

#: Re-exported so a reader (and the unit pin in
#: ``tests/unit/qmd/test_daemon_recycle.py``) can check the value being
#: scaled without having to read the patch line itself.
_POLL_INTERVAL_S = _subprocess._POLL_INTERVAL_S

pytestmark = pytest.mark.slow

#: Scale factor. Every constant is divided by this and the result
#: multiplied back, so the figures below are in real seconds.
N = 100.0

#: The floor, read across several harnesses, with room for their spread.
FLOOR_BAND_S = (27.0, 33.0)

#: The ceiling. Over-estimating is the safe direction for a bound, so the
#: upper end is generous; a reading *below* the band is a surprise worth
#: seeing and is failed deliberately. The upper end has to stay under the
#: unscaled reading (~86-88 s) for the band to have any teeth at all.
CEILING_BAND_S = (52.0, 75.0)

#: The spawn's own ``STATUS_TIMEOUT_S``, the single biggest contributor.
SPAWN_BOUND_S = d.STATUS_TIMEOUT_S

#: Two 2 s grace windows, plus scheduling.
GRACE_COST_S = (2.0, 6.0)

_STATUS_RUNNING = b"MCP:   running (PID 1234)\n"
_STATUS_DOWN = b"no MCP line here\n"

#: Every pipe this harness opens, so the silent-child scenarios cannot
#: leak four descriptors per simulated subprocess call (~60 calls per
#: exhausted budget).
_open_pipes: list[object] = []


class _FakeChild:
    """A process stand-in with real pipes.

    ``_run_qmd`` reads from ``proc.stdout``/``proc.stderr`` with
    ``read1`` and polls them through a ``selectors`` loop. Handing it
    ``os.pipe()`` ends keeps every one of those code paths — including
    the ``sel.select(_POLL_INTERVAL_S)`` timeout, which is the constant
    under test — on the production implementation, while costing nothing
    to construct and guaranteeing no real process is spawned.
    """

    def __init__(self, mode: str) -> None:
        read_out, write_out = os.pipe()
        read_err, write_err = os.pipe()
        self.stdout = os.fdopen(read_out, "rb")
        self.stderr = os.fdopen(read_err, "rb")
        self._writes = [os.fdopen(write_out, "wb"), os.fdopen(write_err, "wb")]
        self.pid = 424242
        self.returncode = 0
        _open_pipes.extend([self.stdout, self.stderr, *self._writes])
        if mode == "hang":
            # Silence, not EOF. A wedged `qmd status` writes nothing and
            # does not exit; closing the write ends would look like a
            # clean completion and the bound would never be reached.
            return
        self._writes[0].write(_STATUS_RUNNING if mode == "up" else _STATUS_DOWN)
        self._writes[0].close()
        self._writes[1].close()

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 0

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:  # noqa: ARG002
        return (b"", b"")

    def poll(self) -> int:
        return 0

    def close_pipes(self) -> None:
        for f in (self.stdout, self.stderr, *self._writes):
            try:
                f.close()
            except OSError:
                pass


def _popen_shim(status_mode: str, spawn_hangs: bool) -> type:  # noqa: ANN202
    """A ``subprocess`` stand-in for ``_run_qmd``, by scenario.

    Returned as a class and patched over ``_subprocess.subprocess``, so
    nothing outside this harness's context manager can see it — patching
    the real module's ``Popen`` in place would be process-global.
    """

    class _Shim:
        PIPE = subprocess.PIPE
        DEVNULL = subprocess.DEVNULL
        CompletedProcess = subprocess.CompletedProcess
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def Popen(args: list[str], **kw: object) -> _FakeChild:  # noqa: N802, ARG004
            is_status = args[1:] == ["status"]
            if is_status:
                mode = {"hangs": "hang", "up": "up"}.get(status_mode, "down")
            else:
                mode = "hang" if spawn_hangs else "down"
            return _FakeChild(mode)

    return _Shim


class _NeverServes:
    """A probe that fails the way a stopped daemon's really does."""

    def __init__(self, url: str) -> None: ...

    async def __aenter__(self) -> _NeverServes:
        raise RuntimeError("Client failed to connect: All connection attempts failed")

    async def __aexit__(self, *args: object) -> None:
        return None

    async def list_tools(self) -> list[object]:
        return []


def _scaled_reap(*, grace: float = 2.0, poll: float = 0.05) -> None:
    """``_reap_qmd_daemon`` with its two grace windows scaled like everything else.

    The control flow is the original's; only the window lengths differ.
    Whether the windows are *reached* is decided upstream, by what the
    faked ``qmd status`` reports — which is exactly the coupling this
    harness exists to measure.
    """
    state = d.qmd_daemon_state()
    if not state.running or state.pid is None:
        return
    deadline = time.monotonic() + grace / N
    while time.monotonic() < deadline:
        if not d._pid_alive(state.pid):
            return
        time.sleep(poll / N)
    deadline = time.monotonic() + grace / N
    while time.monotonic() < deadline:
        if not d._pid_alive(state.pid):
            return
        time.sleep(poll / N)


async def _measure(status_mode: str, spawn_hangs: bool, *, scale_poll: bool = True) -> float:
    """One exhaustion, through the genuine spawn, reap, probe loop and deadline.

    ``scale_poll=False`` is only for the test that proves the rescaling is
    load-bearing; every other reading is taken the way the code runs it.
    """
    _open_pipes.clear()
    poll_patch = (
        patch.object(_subprocess, "_POLL_INTERVAL_S", _subprocess._POLL_INTERVAL_S / N)
        if scale_poll
        else nullcontext()
    )
    with (
        # The real `_run_qmd` — only its `Popen` is replaced, so the
        # reader loop, its bounds and `_POLL_INTERVAL_S` are production.
        patch.object(_subprocess, "subprocess", _popen_shim(status_mode, spawn_hangs)),
        patch.object(d, "STATUS_TIMEOUT_S", SPAWN_BOUND_S / N),
        patch.object(d, "_reap_qmd_daemon", _scaled_reap),
        # The reap's kill is neutered so the fake pid being absent does
        # not short-circuit it; _pid_alive stays True so a "running"
        # status does spend both windows.
        patch.object(d.os, "kill", lambda pid, sig: None),
        patch.object(d, "_pid_alive", lambda pid: True),
        patch.object(d, "write_sidecar_scope", lambda scope: None),
        patch.object(d.fastmcp, "Client", _NeverServes),
        # The reader loop's granularity. Load-bearing: without this line
        # the ceiling reads ~86-88 s instead of ~61 s, out of the band.
        poll_patch,
    ):
        t0 = time.monotonic()
        with pytest.raises(d.QmdRecycleFailed):
            await d.recycle_qmd_daemon(
                scope=Path("/tmp/never-opened"),
                daemon_url="http://127.0.0.1:9/mcp",
                ready_timeout=30.0 / N,
                poll_interval=0.5 / N,
            )
        return (time.monotonic() - t0) * N


#: (label, what qmd status reports, does the spawn hang)
SCENARIOS = (
    ("daemon down", "down", False),
    ("daemon up, ignores SIGTERM", "up", False),
    ("daemon down, spawn hangs", "down", True),
    ("qmd status hangs", "hangs", False),
    ("status and spawn hang", "hangs", True),
)


@pytest.fixture(scope="module")
def readings() -> dict[str, float]:
    """All five scenarios, once. A real exhaustion is slow even scaled."""

    async def _all() -> dict[str, float]:
        return {label: await _measure(mode, hangs) for label, mode, hangs in SCENARIOS}

    out = asyncio.run(_all())
    for f in _open_pipes:
        try:
            f.close()  # type: ignore[attr-defined]
        except OSError:
            pass
    _open_pipes.clear()
    return out


def test_the_poll_granularity_is_reached_and_scaled(readings: dict[str, float]) -> None:
    """The reader loop is real, so ``_POLL_INTERVAL_S`` is read — and matters.

    Three facts, and the test asserts all three rather than reading its
    own source, which is what the previous version did:

    1. **Reached.** ``_run_qmd`` is the production function here, so the
       loop's ``sel.select(_POLL_INTERVAL_S)`` runs on every simulated
       call. Removing the ``_run_qmd`` patch and replacing it with a bare
       stub makes the two readings below identical, and this fails.

    2. **Scaled.** An unscaled 0.1 s survives the 1/100 rescaling and
       costs up to ~10 s of real-scale time per simulated hanging
       ``qmd status`` call.

    3. **Load-bearing at this size.** The ceiling band tops out at 75 s
       and the unscaled ceiling reads ~86-88 s, so a harness that
       stopped scaling the poll cannot sit inside it. Asserting a band
       that the unscaled reading also satisfies would be a pin that
       cannot fail — the exact defect this replaces.
    """
    assert _subprocess._POLL_INTERVAL_S == pytest.approx(0.1)

    # Measure the same ceiling twice: once as the harness runs, once with
    # the rescaling left out. One extra exhaustion at 1/100 scale costs
    # well under a second, and the difference is the whole point.
    unscaled = asyncio.run(_measure("hangs", True, scale_poll=False))
    scaled = readings["status and spawn hang"]

    assert scaled < unscaled - 10.0, (
        f"scaling _POLL_INTERVAL_S changed the ceiling by "
        f"{unscaled - scaled:.1f}s ({scaled:.1f}s scaled vs {unscaled:.1f}s not). "
        f"If this harness no longer runs the real reader loop the constant "
        f"is never read and the two readings are the same measurement."
    )
    assert CEILING_BAND_S[0] <= scaled <= CEILING_BAND_S[1]
    assert unscaled > CEILING_BAND_S[1], (
        f"the unscaled ceiling ({unscaled:.1f}s) has fallen inside "
        f"{CEILING_BAND_S}, so the band no longer proves the poll is scaled. "
        f"Either the harness stopped running the reader loop, or the band's "
        f"upper end was widened past the unscaled reading."
    )


def test_the_floor_is_the_daemon_already_down_case(readings: dict[str, float]) -> None:
    """A stopped daemon is the commonest recycle, and it is the cheapest.

    ``qmd status`` reports not-running, so the reap returns immediately
    without killing anything or spending a grace window.
    """
    floor = readings["daemon down"]
    assert FLOOR_BAND_S[0] <= floor <= FLOOR_BAND_S[1], (
        f"floor measured {floor:.1f}s, expected roughly {FLOOR_BAND_S}"
    )


def test_only_a_running_status_spends_the_grace_windows(
    readings: dict[str, float],
) -> None:
    """Grace is reachable in exactly one configuration, and costs ~4 s there.

    This is the assertion an earlier docstring failed: it said "+up to 4 s"
    and then listed a row whose delta was +5.3 s, belonging to the
    configuration that forgoes the windows entirely.
    """
    down = readings["daemon down"]
    up = readings["daemon up, ignores SIGTERM"]
    delta = up - down
    assert GRACE_COST_S[0] <= delta <= GRACE_COST_S[1], (
        f"the reap's two grace windows cost {delta:.1f}s; two 2s windows "
        f"plus scheduling is the expected magnitude"
    )
    # And the converse: a hanging status reports not-running, so the reap
    # spends no grace window. A figure that added the windows here would
    # be describing a state the code cannot reach.
    #
    # The status calls are not free, though. Each costs a full
    # ``STATUS_TIMEOUT_S`` and they *displace* polling rather than adding
    # to it, so the net is one or two bounds, not three. Which one is
    # genuinely unstable — it turns on whether one more poll fits inside
    # the 30 s budget, and a status call is 15 s wide, so 30/15 is
    # exactly two and the answer sits on the boundary. That is a
    # property of the code, not noise to be averaged away.
    hanging = readings["qmd status hangs"]
    delta = hanging - down
    assert SPAWN_BOUND_S - 3.0 <= delta <= 2 * SPAWN_BOUND_S, (
        f"a hanging qmd status cost {delta:.1f}s over the floor; expected "
        f"one to two {SPAWN_BOUND_S:g}s bounds, since its calls displace "
        f"polling time rather than adding to it, and how many fit depends "
        f"on where the deadline lands"
    )


def test_the_spawn_bound_is_the_biggest_single_contributor(
    readings: dict[str, float],
) -> None:
    """The spawn has its own ``STATUS_TIMEOUT_S``, and no summary ever named it.

    Its cost is the same whether or not the status hangs — the spawn's
    bound is paid either way — which is why it dominates the middle.
    """
    down = readings["daemon down"]
    spawn_hang = readings["daemon down, spawn hangs"]
    delta = spawn_hang - down
    assert SPAWN_BOUND_S - 3.0 <= delta <= SPAWN_BOUND_S + 3.0, (
        f"spawn-hang cost {delta:.1f}s over the floor; expected roughly the "
        f"spawn's own {SPAWN_BOUND_S:g}s STATUS_TIMEOUT_S"
    )


def test_the_ceiling_is_the_worst_row_and_is_bounded(
    readings: dict[str, float],
) -> None:
    """The reported ceiling is the maximum over the scenarios, not a number
    chosen for effect."""
    ceiling_key = "status and spawn hang"
    ceiling = readings[ceiling_key]
    assert ceiling == max(readings.values()), (
        f"'{ceiling_key}' is not the worst scenario: {readings}"
    )
    assert CEILING_BAND_S[0] <= ceiling <= CEILING_BAND_S[1], (
        f"ceiling measured {ceiling:.1f}s, outside {CEILING_BAND_S}. "
        f"Over-estimating is safe for a bound; under-estimating is not."
    )


def test_the_figures_are_reported(readings: dict[str, float]) -> None:
    """Print them, so a changed measurement is visible in CI output."""
    width = max(len(k) for k in readings)
    for label, value in readings.items():
        print(f"{label:<{width}}  {value:6.1f} s  (real scale)")
    assert set(readings) == {label for label, _, _ in SCENARIOS}
