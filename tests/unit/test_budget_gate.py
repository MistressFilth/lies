"""The budget gate must name *why* it failed, not just that it did."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from tests.unit import conftest as gate

# --- the classification result -------------------------------------------


def test_a_clean_remeasure_is_measured() -> None:
    assert gate._Remeasure(durations={"t": 0.01}).measured
    assert gate._Remeasure(durations={}).measured


def test_a_bail_is_not_measured_and_carries_its_cause() -> None:
    result = gate._Remeasure(unavailable_reason="the re-run process failed", detail="traceback")
    assert not result.measured
    assert result.durations == {}
    assert "the re-run process failed" in result.unavailable_reason
    assert "traceback" in result.detail


# --- subprocess classification -------------------------------------------


def _stub_run(returncode: int, stdout: str = "", stderr: str = ""):
    def fake_run(*_args: object, **_kwargs: object):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr
        )

    return fake_run


def test_a_failing_rerun_quotes_stderr_not_just_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collection error lands on stderr, and stderr is the whole story."""
    monkeypatch.setattr(
        subprocess,
        "run",
        _stub_run(
            2,
            stdout="some noise on stdout\n",
            stderr="ImportError: cannot import name 'x' from 'lies.foo'\n",
        ),
    )
    result = gate._remeasure_in_isolation(["tests/unit/test_x.py::test_y"])
    assert not result.measured
    assert "the re-run process failed" in result.unavailable_reason
    assert "ImportError: cannot import name" in result.detail
    assert "re-run stderr" in result.detail


def test_a_successful_rerun_returns_durations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _stub_run(0, stdout='LIES_BUDGET_JSON={"tests/unit/test_x.py::test_y": 0.42}\n'),
    )
    result = gate._remeasure_in_isolation(["tests/unit/test_x.py::test_y"])
    assert result.measured
    assert result.durations == {"tests/unit/test_x.py::test_y": 0.42}
    assert result.unavailable_reason == ""


def test_a_rerun_with_no_duration_line_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _stub_run(0, stdout="nothing useful\n"))
    result = gate._remeasure_in_isolation(["tests/unit/test_x.py::test_y"])
    assert not result.measured
    assert "no duration line" in result.unavailable_reason


def test_a_timeout_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(subprocess, "run", explode)
    result = gate._remeasure_in_isolation(["tests/unit/test_x.py::test_y"])
    assert not result.measured
    assert "exceeded" in result.unavailable_reason


def test_the_rerun_bound_scales_with_the_batch() -> None:
    """The re-run bound scales with the batch size."""
    one = gate._isolation_timeout_s(1)
    many = gate._isolation_timeout_s(50)
    assert one == gate._ISOLATION_BASE_S + gate._ISOLATION_PER_TEST_S
    assert many > one, "a 50-test batch must get more than a 1-test batch"
    assert many - one == 49 * gate._ISOLATION_PER_TEST_S
    # An empty batch is not a request to run with no allowance.
    assert gate._isolation_timeout_s(0) == one


def test_the_bound_reaches_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The derived bound is the one actually handed to the subprocess."""
    seen: dict[str, object] = {}

    def capture(*_args: object, **kwargs: object):
        seen.update(kwargs)
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="LIES_BUDGET_JSON={}\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", capture)
    nodeids = [f"tests/unit/test_x.py::test_{i}" for i in range(20)]
    gate._remeasure_in_isolation(nodeids)
    assert seen["timeout"] == gate._isolation_timeout_s(20, repeats=gate._ISOLATION_REPEATS)


class _ExitRecorder:
    """Catch ``pytest.exit`` where it happens.

    ``pytest.exit`` raises ``_pytest.outcomes.Exit``, which is *not* a
    ``SystemExit`` -- catching that instead lets the exception escape and
    aborts the whole run mid-file, which is how the first draft of this
    file appeared to "pass" while quietly skipping its own cases.
    """

    # Captured before the test patches ``pytest.exit``, which is this
    # very attribute -- looking it up inside __call__ after the patch
    # finds the recorder instead of the class.
    _EXIT = pytest.exit.Exception

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.message = ""

    def __call__(self, msg: str = "", returncode: int = 0) -> None:
        self.message = msg
        self.returncode = returncode
        raise self._EXIT(msg)

    def __enter__(self) -> _ExitRecorder:
        return self

    def __exit__(self, exc_type: object, *_exc: object) -> bool:
        # Swallow the Exit the recorder raised, which is the whole
        # point: an uncaught one aborts the run and the cases after it
        # never execute.
        return exc_type is self._EXIT


class _RecordingReporter:
    """Just enough of ``TerminalReporter`` for the gate to write to."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_line(self, text: str = "", **_kwargs: Any) -> None:
        self.lines.append(text)

    def write_sep(self, _sep: str, title: str, **_kwargs: Any) -> None:
        self.lines.append(title)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _breaches() -> dict[str, tuple[float, bool]]:
    return {"tests/unit/test_x.py::test_y": (0.40, False)}


def test_an_unavailable_remeasure_does_not_print_the_timing_rubric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test that errored is not reported as too slow."""
    monkeypatch.setattr(gate, "_call_durations", _breaches())
    monkeypatch.setattr(gate, "_GATE_DISABLED", False)
    monkeypatch.setattr(
        gate,
        "_remeasure_in_isolation",
        lambda *a, **k: gate._Remeasure(
            unavailable_reason="the re-run process failed",
            detail="ImportError: cannot import name 'x'",
        ),
    )
    reporter = _RecordingReporter()
    with _ExitRecorder() as exit_:
        monkeypatch.setattr(gate.pytest, "exit", exit_)
        gate.pytest_terminal_summary(reporter, 0, None)  # type: ignore[arg-type]
    text = reporter.text
    assert exit_.returncode == 1, exit_.message
    assert "could not verify" in exit_.message
    assert "could not be performed" in text
    assert "ImportError" in text
    assert "NOT a timing verdict" in text
    # The two things that made the old report misleading.
    assert "HARD LIMIT VIOLATIONS" not in text
    assert "Remediation rubric" not in text


def test_a_confirmed_slow_test_still_prints_the_rubric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real case must keep its report; only the bail path changed."""
    monkeypatch.setattr(gate, "_call_durations", _breaches())
    monkeypatch.setattr(gate, "_GATE_DISABLED", False)
    monkeypatch.setattr(
        gate,
        "_remeasure_in_isolation",
        lambda *a, **k: gate._Remeasure(durations={"tests/unit/test_x.py::test_y": 0.40}),
    )
    reporter = _RecordingReporter()
    with _ExitRecorder() as exit_:
        monkeypatch.setattr(gate.pytest, "exit", exit_)
        gate.pytest_terminal_summary(reporter, 0, None)  # type: ignore[arg-type]
    text = reporter.text
    assert exit_.returncode == 1, exit_.message
    assert "exceeded the" in exit_.message
    assert "HARD LIMIT VIOLATIONS" in text
    assert "Remediation rubric" in text
    assert "could not be performed" not in text


def test_noise_clearing_the_limit_passes_without_the_rubric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A breach that clears in isolation is noise, and the run passes."""
    monkeypatch.setattr(gate, "_call_durations", _breaches())
    monkeypatch.setattr(gate, "_GATE_DISABLED", False)
    monkeypatch.setattr(
        gate,
        "_remeasure_in_isolation",
        lambda *a, **k: gate._Remeasure(durations={"tests/unit/test_x.py::test_y": 0.02}),
    )
    reporter = _RecordingReporter()
    gate.pytest_terminal_summary(reporter, 0, None)  # type: ignore[arg-type]
    text = reporter.text
    assert "in-suite noise, not cost" in text
    assert "Run passes" in text
    assert "Remediation rubric" not in text


@pytest.mark.slow
def test_the_gate_still_uses_the_real_subprocess_for_a_real_measurement() -> None:
    """Guard the seam the stubs above replace."""
    probe = "print('LIES_BUDGET_JSON=' + '{}')"
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "LIES_BUDGET_JSON=" in proc.stdout
    # The probe file the gate writes is what produces that line; assert
    # the string it greps for is still the one it emits.
    source = (sys.modules[gate.__name__].__file__ or "").replace(".pyc", ".py")
    assert "LIES_BUDGET_JSON=" in open(source, encoding="utf-8").read()
