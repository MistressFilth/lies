"""Measure the recycle's worst case, and assert only its STRUCTURE.

Every time constant in the path is scaled by ``1/N`` so a real exhaustion
finishes in seconds, then the measured wall time is scaled back. N = 100
puts the floor at ~0.31 s and the ceiling at ~0.62 s.

**What is scaled.** ``STATUS_TIMEOUT_S``, ``ready_timeout``,
``poll_interval``, the reap's ``grace``/``poll``, **and**
``_subprocess._POLL_INTERVAL_S``. The last one is neither a timeout nor a
sleep — it is the granularity of the reader loop's ``sel.select()`` — so
it does not scale by itself, and an unscaled 0.1 s lands as **up to ~6 s
per simulated hanging ``qmd status`` call** in real-scale terms. An
earlier version of this harness left it alone and inflated every
hanging-status row by more than the bound it was quoting.

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
path and disagree at the 1–4 s level, because they differ in what they
treat as scaled. The decimals belong to a harness, not to the code. What
belongs to the code is the shape: a floor, a ceiling, the spawn's own
bound dominating the middle, and grace windows spent only where they can
be. Those are asserted; the figures are reported.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from lies.qmd import _subprocess
from lies.qmd import daemon as d

pytestmark = pytest.mark.slow

#: Scale factor. Every constant is divided by this and the result
#: multiplied back, so the figures below are in real seconds.
N = 100.0

#: The floor, read across several harnesses, with room for their spread.
FLOOR_BAND_S = (27.0, 33.0)

#: The ceiling. Over-estimating is the safe direction for a bound, so the
#: upper end is generous; a reading *below* the band is a surprise worth
#: seeing and is failed deliberately.
CEILING_BAND_S = (52.0, 75.0)

#: The spawn's own ``STATUS_TIMEOUT_S``, the single biggest contributor.
SPAWN_BOUND_S = d.STATUS_TIMEOUT_S

#: Two 2 s grace windows, plus scheduling.
GRACE_COST_S = (2.0, 6.0)

_STATUS_RUNNING = b"MCP:   running (PID 1234)\n"
_STATUS_DOWN = b"no MCP line here\n"


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


def _fake_run_qmd(status_mode: str, spawn_hangs: bool):  # type: ignore[no-untyped-def]
    """A subprocess stand-in for both ``qmd status`` and the spawn."""
    status_bound = d.STATUS_TIMEOUT_S / N

    def _run(args, cwd, timeout, **kw):  # type: ignore[no-untyped-def]
        if args[1:] == ["status"]:
            if status_mode == "hangs":
                time.sleep(status_bound)
                # No parseable MCP line -> qmd_daemon_state says
                # running=False -> the reap returns without killing.
                return subprocess.CompletedProcess(args, 0, b"", b"")
            body = _STATUS_RUNNING if status_mode == "up" else _STATUS_DOWN
            return subprocess.CompletedProcess(args, 0, body, b"")
        # This IS the spawn. _spawn_qmd_daemon is deliberately not
        # stubbed: an earlier version of this harness stubbed it, which
        # silently deleted the spawn-hang scenario under test.
        if spawn_hangs:
            time.sleep(timeout / N)
        return subprocess.CompletedProcess(args, 0, b"Started\n", b"")

    return _run


async def _measure(status_mode: str, spawn_hangs: bool) -> float:
    """One exhaustion, through the genuine spawn, reap, probe loop and deadline."""
    with (
        patch.object(d, "_run_qmd", _fake_run_qmd(status_mode, spawn_hangs)),
        patch.object(d, "_reap_qmd_daemon", _scaled_reap),
        # The reap's kill is neutered so the fake pid being absent does
        # not short-circuit it; _pid_alive stays True so a "running"
        # status does spend both windows.
        patch.object(d.os, "kill", lambda pid, sig: None),
        patch.object(d, "_pid_alive", lambda pid: True),
        patch.object(d, "write_sidecar_scope", lambda scope: None),
        patch.object(d.fastmcp, "Client", _NeverServes),
        patch.object(_subprocess, "_POLL_INTERVAL_S", _subprocess._POLL_INTERVAL_S / N),
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

    return asyncio.run(_all())


def test_the_poll_granularity_is_scaled() -> None:
    """The one constant that is neither a timeout nor a sleep is scaled too.

    ``_subprocess._POLL_INTERVAL_S`` is the reader loop's ``sel.select()``
    granularity. Left unscaled it survives a 1/100 rescaling and lands as
    up to ~6 s per simulated hanging ``qmd status`` call - more than the
    bound being quoted.

    Two halves, and the second is the one that matters. Asserting the
    constant is still 0.1 only says the *source* is unchanged; asserting
    that this harness patches it is what stops the measurement from
    quietly reverting to the version a reviewer rejected. The patch is
    inspected on the module rather than the behaviour, because the
    symptom is inflated numbers that still sit inside a band.
    """
    assert _subprocess._POLL_INTERVAL_S == pytest.approx(0.1)
    source = Path(__file__).read_text(encoding="utf-8")
    assert (
        'patch.object(_subprocess, "_POLL_INTERVAL_S", _subprocess._POLL_INTERVAL_S / N)' in source
    ), (
        "this harness no longer scales _subprocess._POLL_INTERVAL_S. It is "
        "neither a timeout nor a sleep, so it does not scale by itself, and "
        "leaving it alone inflates every hanging-qmd-status row by up to "
        "~6s per simulated call."
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
    # genuinely unstable — measured 17.7 s and 19.7 s on consecutive
    # runs — because it turns on whether one more poll fits inside the
    # 30 s budget, and a status call is 15 s wide. That is a property of
    # the code, not noise to be averaged away, so the band spans both.
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
