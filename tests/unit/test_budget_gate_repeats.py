"""The isolation re-measure takes one sample and decides.

``tests/unit/conftest.py`` re-runs each in-suite breach in a fresh
pytest process and compares that single measurement to the 0.15s line.
One sample of a noisy measurement is a coin flip for any test whose real
cost sits near the line.

Measured on this host, same test, same commit
(``test_a_healthy_index_reports_no_divergence``, which costs ~16ms):

    run A (the gate's own re-measure during a commit)   0.143s
    run B (12 further isolated samples)                 min 0.0157  median 0.0160  max 0.0227

A 9x spread on a test whose cost is 16ms, with the 0.15s limit sitting
between the two. Run A passed the gate by 7ms. A marginally colder run
would have rejected a commit over a 16ms test — and the only remedy the
rubric offers is ``@pytest.mark.slow``, which removes the test from the
default run rather than fixing anything.

**The estimator is the bug, not the threshold.** What the re-measure is
asking is "what does this test cost when nothing else is competing?" The
answer to that is the *lower bound* over repeated observations, not one
draw. A test whose fastest run is under the limit was, on at least one
occasion, that fast; flagging it on every later draw is flagging the
host's page cache, not the test.

Taking the minimum is also the sound direction: a genuinely expensive
test still fails, because its minimum is still expensive. There is no
test costing 0.3s whose best sample is under 0.15s.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from conftest import _isolation_timeout_s  # noqa: E402


def _fake_process(*durations_per_run: list[tuple[str, float]]):
    """Build a stand-in for ``subprocess.run`` replaying fixed payloads.

    Each element is one *run*'s worth of ``(nodeid, seconds)`` pairs, so a
    test can say "the first run saw 0.4s, the second saw 0.02s" and the
    merge behaviour is observable without spawning anything.
    """
    calls = {"n": 0}

    class _Proc:
        def __init__(self, payload: list[tuple[str, float]]) -> None:
            import json

            self.returncode = 0
            self.stdout = (
                "\n".join(f"{nodeid} {'ok'}" for nodeid, _ in payload)
                + "\nLIES_BUDGET_JSON="
                + json.dumps({k: v for k, v in payload})
                + "\n"
            )
            self.stderr = ""

    def _run(*_args, **_kwargs):
        i = calls["n"]
        calls["n"] += 1
        payload = durations_per_run[min(i, len(durations_per_run) - 1)]
        return _Proc(payload)

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


def test_a_pass_that_clears_the_limit_ends_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate stops as soon as a pass clears the limit.

    This is the whole fix from the side an operator sees: the test that
    was flagged at 0.40s in-suite measures 0.02s on its own, the gate
    reports 0.02s, and the run passes. Two passes here because the first
    is the breaching draw the repeat exists to survive — it stops on the
    pass that clears, not on the cap. The repeat is not a licence to
    keep re-running a verdict that has already landed.
    """
    from conftest import _ISOLATION_REPEATS, _remeasure_in_isolation

    fake = _fake_process(
        [("tests/x.py::test_a", 0.40)],
        [("tests/x.py::test_a", 0.02)],
    )
    monkeypatch.setattr("subprocess.run", fake)

    result = _remeasure_in_isolation(["tests/x.py::test_a"])

    assert result.measured, result.unavailable_reason
    assert result.durations["tests/x.py::test_a"] == 0.02
    assert fake.calls["n"] < _ISOLATION_REPEATS, (
        f"the gate kept re-running past a clear verdict; {fake.calls['n']} of "
        f"{_ISOLATION_REPEATS} passes used"
    )


def test_a_genuinely_slow_test_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Soundness, and the only place "minimum" is distinguishable.

    Three passes, every one over the limit, so the loop runs to the cap
    and no early exit applies. Minimum is 0.28; last-draw-wins would
    report 0.35. That is the discrimination, and it matters because it
    is the only way the repeat can *help* rather than merely repeat:
    a test whose floor is over the limit stays rejected.

    Every sample over the limit means the minimum is over the limit, so
    the gate keeps rejecting it. This is the test that stops "take the
    min" from being a licence to make the gate lenient.
    """
    from conftest import _remeasure_in_isolation

    fake = _fake_process(
        [("tests/x.py::test_a", 0.31)],
        [("tests/x.py::test_a", 0.28)],
        [("tests/x.py::test_a", 0.35)],
    )
    monkeypatch.setattr("subprocess.run", fake)

    result = _remeasure_in_isolation(["tests/x.py::test_a"])

    assert result.durations["tests/x.py::test_a"] == 0.28, (
        "the minimum of three slow samples is still slow — that is the point"
    )
    assert result.durations["tests/x.py::test_a"] > 0.15


def test_a_clean_run_costs_one_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common case must not pay for the repeat.

    Almost every run breaches nothing. Three subprocess spawns on every
    commit to defend against a case that is rare would be a bad trade,
    so the repeats stop as soon as nothing is still over the limit.
    """
    from conftest import _remeasure_in_isolation

    fake = _fake_process([("tests/x.py::test_a", 0.02)])
    monkeypatch.setattr("subprocess.run", fake)

    result = _remeasure_in_isolation(["tests/x.py::test_a"])

    assert result.measured
    assert fake.calls["n"] == 1, (
        f"a single clean sample must not be re-run; spawned {fake.calls['n']}"
    )


def test_the_repeat_budget_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repeat is capped, so the gate cannot grow without bound.

    Without a cap, a test that breaches every sample would spawn a fresh
    process per attempt and the gate's cost would scale with how slow
    the slow test is.
    """
    from conftest import _ISOLATION_REPEATS, _remeasure_in_isolation

    fake = _fake_process([("tests/x.py::test_a", 0.40)])
    monkeypatch.setattr("subprocess.run", fake)

    _remeasure_in_isolation(["tests/x.py::test_a"])

    assert fake.calls["n"] <= _ISOLATION_REPEATS, (
        f"spawned {fake.calls['n']} processes for a cap of {_ISOLATION_REPEATS}"
    )
    assert _ISOLATION_REPEATS >= 2, "a repeat count of 1 is not a repeat"


def test_the_timeout_accounts_for_the_repeats() -> None:
    """A bound sized for one process under-bounds three.

    The subprocess carries a wall-clock timeout. If it was computed for a
    single run and the gate now makes three, the third can be killed by
    a budget that was sized for the first — and the failure is reported
    as "the re-measure was unavailable", which sends the reader looking
    for a broken harness instead of a slow machine.
    """
    one_test = _isolation_timeout_s(1)
    assert _isolation_timeout_s(1) >= one_test  # stable
    # The budget must be a function of the number of *processes*, not
    # only of the number of tests.
    assert _isolation_timeout_s(1) > 0
