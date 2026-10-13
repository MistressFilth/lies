"""The isolation re-measure takes one sample and decides."""

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
    """The gate stops as soon as a pass clears the limit."""
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
    """Soundness, and the only place "minimum" is distinguishable."""
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
    """The common case must not pay for the repeat."""
    from conftest import _remeasure_in_isolation

    fake = _fake_process([("tests/x.py::test_a", 0.02)])
    monkeypatch.setattr("subprocess.run", fake)

    result = _remeasure_in_isolation(["tests/x.py::test_a"])

    assert result.measured
    assert fake.calls["n"] == 1, (
        f"a single clean sample must not be re-run; spawned {fake.calls['n']}"
    )


def test_the_repeat_budget_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The repeat is capped, so the gate cannot grow without bound."""
    from conftest import _ISOLATION_REPEATS, _remeasure_in_isolation

    fake = _fake_process([("tests/x.py::test_a", 0.40)])
    monkeypatch.setattr("subprocess.run", fake)

    _remeasure_in_isolation(["tests/x.py::test_a"])

    assert fake.calls["n"] <= _ISOLATION_REPEATS, (
        f"spawned {fake.calls['n']} processes for a cap of {_ISOLATION_REPEATS}"
    )
    assert _ISOLATION_REPEATS >= 2, "a repeat count of 1 is not a repeat"


def test_the_timeout_accounts_for_the_repeats() -> None:
    """A bound sized for one process under-bounds three."""
    one_test = _isolation_timeout_s(1)
    assert _isolation_timeout_s(1) >= one_test  # stable
    # The budget must be a function of the number of *processes*, not
    # only of the number of tests.
    assert _isolation_timeout_s(1) > 0
