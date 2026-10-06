"""Unit-test-only fixtures and options.

Adds ``--runslow``: when absent, every test marked ``@pytest.mark.slow``
is skipped. The default ``make unit-test`` run skips them; CI's
``--runslow`` mode (or a developer chasing a regression) re-enables
them. The marker is registered in ``pyproject.toml``.

Enforces the 0.15s hard limit: any non-slow-marked test whose
``call`` phase exceeds ``HARD_LIMIT_S`` fails the run with the
remediation rubric printed. Slow-marked tests are exempt (they run
only with ``--runslow`` and are explicitly opt-in to higher cost).
The pre-commit gate is satisfied because ``make unit-test`` (run by
the pre-commit ``test`` hook) inherits the failure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

HARD_LIMIT_S = 0.15
# Bound on the second, isolated re-measurement pass, split into a floor
# for interpreter start and collection plus a per-test allowance.
# A single fixed bound was wrong in both directions. High enough to
# cover any realistic batch it is also high enough that one wedged
# test hangs the gate for two minutes; low enough for a typical one it
# fires on a loaded machine and converts what should have been a noise
# verdict into a hard failure — the failure mode the re-measure exists
# to prevent.
_ISOLATION_BASE_S = 60
_ISOLATION_PER_TEST_S = 5
# How many isolated passes the gate makes before giving up on clearing a
# breach. One pass is one draw from a noisy distribution, and this gate
# sits close enough to the scheduler's noise floor that the draw decides
# the verdict: measured on this host, the same test on the same commit
# measured 0.143s in one pass and 0.016s across twelve more, with the
# 0.15s limit between them. A test costing 16ms passed by 7ms.
#
# The verdict is the *minimum* over the passes, because the question the
# re-measure asks is what the test costs when nothing else competes, and
# the answer to that is a floor rather than a draw. The direction is
# sound: a genuinely expensive test still fails, because its fastest
# pass is still expensive.
_ISOLATION_REPEATS = 3


def _isolation_timeout_s(count: int, repeats: int = 1) -> float:
    """Wall-clock bound for re-running ``count`` tests, ``repeats`` times.

    Sized by *processes*, not just tests. A bound computed for one pass
    under-bounds the second, the killed pass is reported as "the
    re-measure was unavailable", and the reader goes looking for a broken
    harness instead of a slow machine.
    """
    return (repeats or 1) * (_ISOLATION_BASE_S + _ISOLATION_PER_TEST_S * max(count, 1))


# CI runs the full test suite (``make test`` with ``--runslow`` and
# ``INTEGRATION=1``) and is not the place to enforce per-test
# timing — wall-clock variance across CI runners would flake the gate.
# Pre-commit (which fires locally with a fixed runner) keeps the
# enforcement. Set ``LIES_SKIP_BUDGET_GATE=1`` to disable the gate for
# ad-hoc profiling (``uv run pytest tests/unit/ --runslow LIES_SKIP_BUDGET_GATE=1``).
_GATE_DISABLED = os.environ.get("LIES_SKIP_BUDGET_GATE") == "1" or os.environ.get("CI") == "true"


# Pre-imports disabled — the original ``lies.qmd.capability`` pre-import
# (intended to amortise the 2s pydantic_ai/fastmcp load cost) interacts
# poorly with TestModel-based agent tests in the full suite:
# ``synthesizer invoked more times than canned answers`` errors fire when
# the TestModel's ``_structured_response_messages`` queue is consumed by
# upstream state from the pre-import chain. The cost is paid once per
# session instead, in whichever test runs first; the gate accommodates
# that via per-test slow marks on the affected CLI tests.

# import lies.qmd.capability  # noqa: E402, F401  # disabled — see note


@pytest.fixture(autouse=True)
def _stub_qmd_recycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the qmd daemon liveness probe for every test in ``tests/unit/``.

    Construction of ``QmdCapability`` calls ``_probe_liveness(url)``
    against the dev environment's running qmd daemon (e.g. on
    127.0.0.1:8181). Without this stub, a developer machine with a
    healthy daemon would still open a real TCP socket + JSON-RPC
    session from every orchestrator test — flake-prone across machines
    that have *no* daemon (probe raises, tests fall back, but the
    import path itself depends on daemon state). Tests in this
    directory don't exercise qmd probe/recycle behavior — that's
    ``test_qmd_capability.py``'s job. Stubbing the probe at the
    conftest level covers every orchestrator-using file
    (``test_orchestrator*``, ``test_orchestrator_lint``, etc.) and any
    future orchestrator test added here.

    Also stub ``qmd_daemon_reachable`` so the TCP-level check returns
    True, so the stubbed probe is actually exercised.
    """
    from lies.qmd.daemon import QmdState

    async def _stub_probe(url: str) -> None:
        return None

    async def _stub_recycle(*, data_dir: Path, daemon_url: str, **kwargs: object) -> QmdState:
        return QmdState(True, True, 1, "test stub")

    monkeypatch.setattr("lies.qmd.capability._probe_liveness", _stub_probe)
    monkeypatch.setattr(
        "lies.qmd.capability.qmd_daemon_reachable",
        lambda url, timeout=0.5: True,
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip slow-marked items unless ``--runslow`` was passed."""
    if config.getoption("--runslow"):
        return
    skip_slow = pytest.mark.skip(reason="slow test; run with --runslow to enable")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


# ---------------------------------------------------------------------------
# Hard-limit enforcement
# ---------------------------------------------------------------------------
# Per-nodeid: (call-phase duration in seconds, is_slow-marked). Populated
# by the ``pytest_runtest_makereport`` wrapper below; consumed by
# ``pytest_terminal_summary`` to fail the run when any non-slow-marked
# test breaches ``HARD_LIMIT_S``.
_call_durations: dict[str, tuple[float, bool]] = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> object:
    outcome = yield
    if call.when == "call":
        _call_durations[item.nodeid] = (call.duration, "slow" in item.keywords)
    return outcome.get_result()


def _tail(stream: str, label: str, lines: int = 20) -> str:
    """The last ``lines`` of ``stream``, indented, or nothing if empty.

    The budget gate's whole report is a timing verdict, and a developer
    reading a failure needs the *cause* to be in it rather than one
    scrollable line above the rubric. Import errors and assertion
    failures are multi-line, so the tail is what carries them.
    """
    rows = [row for row in stream.splitlines() if row.strip()]
    if not rows:
        return ""
    body = "\n".join(f"      {row}" for row in rows[-lines:])
    return f"\n    --- re-run {label} (last {min(len(rows), lines)} line(s)) ---\n{body}\n"


@dataclass(frozen=True)
class _Remeasure:
    """The outcome of one isolation re-measurement pass.

    The two failure shapes are deliberately distinct rather than both
    collapsing to an empty dict. A re-run that *could not happen* — a
    broken import in the test, a flake, a timeout, a harness that
    stopped working — says nothing about how long the test takes, and
    reporting it as a timing violation tells the reader to go make the
    test faster when the real problem is that it errored. The caller
    needs to tell them apart to name the right thing.

    ``durations`` is empty whenever ``unavailable_reason`` is set.
    """

    durations: dict[str, float] = field(default_factory=dict)
    unavailable_reason: str = ""
    detail: str = ""

    @property
    def measured(self) -> bool:
        return not self.unavailable_reason


def _remeasure_in_isolation(
    nodeids: list[str], terminalreporter: pytest.TerminalReporter | None = None
) -> _Remeasure:
    """Re-run ``nodeids`` in fresh pytest processes and return their
    call-phase durations.

    Up to ``_ISOLATION_REPEATS`` passes, keeping the **minimum** duration
    per test and stopping early once nothing is still over the limit. See
    ``_ISOLATION_REPEATS`` for why one draw is not enough and why the
    minimum is the sound estimator.

    A full-suite run measures each test under contention: scheduler
    latency and GC pauses land on whichever test happens to be running,
    and the 0.15s line sits close enough to the noise floor that a test
    whose body is instantaneous gets flagged. The only reliable
    discriminator is the same test measured on its own, where nothing
    else is competing for the GIL.

    Only the breaching tests are re-run, so a clean suite costs one
    measurement pass and a noisy one costs one more short process.

    Returns an *unavailable* result when the re-run cannot be performed
    (no ``pytest`` importable, a timeout, a parse failure) so a broken
    measurement harness cannot silently pass a real breach. Every such
    path names itself on the terminal first: without it, a harness that
    stopped working and a re-run that genuinely cleared the limit are
    indistinguishable from outside, and the next person to hit it is
    reading a failure the gate cannot explain.
    """
    import json
    import os
    import subprocess
    import sys
    import tempfile

    def bail(reason: str, detail: str = "") -> _Remeasure:
        if terminalreporter is not None:
            terminalreporter.write_line(
                f"  [budget gate] isolation re-measure unavailable: {reason}"
                + (f" — {detail}" if detail else ""),
                red=True,
            )
        return _Remeasure(unavailable_reason=reason, detail=detail)

    if not nodeids:
        return _Remeasure()
    reporter = '''
"""Emitted by the budget gate's isolation re-run; see tests/unit/conftest.py."""

import json

import pytest

_durations = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if call.when == "call":
        _durations[item.nodeid] = call.duration
    return outcome.get_result()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    print("LIES_BUDGET_JSON=" + json.dumps(_durations))
'''
    best: dict[str, float] = {}
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "_lies_budget_probe.py").write_text(reporter, encoding="utf-8")
        env = dict(os.environ, LIES_SKIP_BUDGET_GATE="1", PYTEST_ADDOPTS="")
        env["PYTHONPATH"] = os.pathsep.join(
            [tmp, *([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])]
        )
        env.pop("PYTEST_CURRENT_TEST", None)

        for attempt in range(1, _ISOLATION_REPEATS + 1):
            timeout_s = _isolation_timeout_s(len(nodeids), repeats=_ISOLATION_REPEATS)
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        *nodeids,
                        "-p",
                        "no:randomly",
                        "-p",
                        "no:cacheprovider",
                        "-p",
                        "_lies_budget_probe",
                        "-q",
                        "--no-header",
                        "-s",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    env=env,
                    cwd=Path.cwd(),
                )
            except subprocess.TimeoutExpired:
                return bail(
                    f"the re-run exceeded its {timeout_s:.0f}s bound "
                    f"({len(nodeids)} test(s) at "
                    f"{_ISOLATION_BASE_S:.0f}s + {_ISOLATION_PER_TEST_S:.0f}s each, "
                    f"x{_ISOLATION_REPEATS} passes)"
                )
            except OSError as exc:
                return bail("the re-run process could not start", str(exc))
            if proc.returncode != 0:
                # A non-zero exit here is usually NOT a slow test — it is the
                # test erroring in a fresh process (a broken import, a fixture
                # that needs a real network), which is the one thing the
                # in-suite run already passed. Both streams are quoted: a
                # collection error lands on stderr, and the old code read only
                # stdout, so the detail it showed was usually the wrong (or an
                # empty) one.
                return bail(
                    "the re-run process failed",
                    f"exit {proc.returncode}\n{_tail(proc.stderr, 'stderr')}"
                    f"{_tail(proc.stdout, 'stdout')}",
                )
            parsed: dict[str, float] | None = None
            for line in proc.stdout.splitlines():
                if line.startswith("LIES_BUDGET_JSON="):
                    try:
                        raw = json.loads(line.removeprefix("LIES_BUDGET_JSON="))
                    except json.JSONDecodeError as exc:
                        return bail("the re-run emitted unparseable durations", str(exc))
                    parsed = {k: float(v) for k, v in raw.items()}
                    break
            if parsed is None:
                return bail("the re-run emitted no duration line")

            for nodeid, seconds in parsed.items():
                prior = best.get(nodeid)
                best[nodeid] = seconds if prior is None else min(prior, seconds)

            # Nothing left over the limit means the remaining passes would
            # only ever lower a number that is already in the clear, so
            # stop. Almost every run breaches nothing and pays one spawn.
            if not any(v > HARD_LIMIT_S for v in best.values()):
                break
            if attempt < _ISOLATION_REPEATS and terminalreporter is not None:
                terminalreporter.write_line(
                    f"  [budget gate] pass {attempt}/{_ISOLATION_REPEATS}: "
                    f"{sum(1 for v in best.values() if v > HARD_LIMIT_S)} test(s) still over "
                    f"{HARD_LIMIT_S:.2f}s; re-measuring",
                )
    return _Remeasure(durations=best)


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Fail the run when a non-slow-marked test genuinely exceeds the
    hard limit.

    An in-suite breach is re-measured in isolation before it counts. A
    test that only breaches because the full suite was contending around
    it is reported as noise and the run passes; a test that breaches on
    its own is a real cost and fails the run with the remediation
    rubric. Without that second measurement the gate punishes exactly
    the tests that are cheapest in isolation, and the only remedy
    available to the author -- a ``@pytest.mark.slow`` mark -- removes
    the test from the default run entirely rather than fixing anything.

    The pre-commit ``test`` hook (which invokes ``make unit-test``)
    inherits the failure, so a commit that adds a real regression is
    rejected. Slow-marked tests are exempt and are not re-measured:
    they run only with ``--runslow`` and are explicitly opt-in to higher
    cost.

    Disabled in CI (``CI=true``) and when ``LIES_SKIP_BUDGET_GATE=1``
    is set — CI runs the full suite without timing enforcement.
    """
    if _GATE_DISABLED:
        return
    breaches = sorted(
        (
            (duration, nodeid)
            for nodeid, (duration, is_slow) in _call_durations.items()
            if duration > HARD_LIMIT_S and not is_slow
        ),
        key=lambda pair: -pair[0],
    )
    if not breaches:
        return

    isolated = _remeasure_in_isolation(
        [nodeid for _, nodeid in breaches], terminalreporter=terminalreporter
    )
    if not isolated.measured:
        # The measurement never happened. Falling through to the timing
        # report here is what produced the confusing failure: a test
        # that *errored* in a fresh process was reported as a test that
        # was too slow, with the actual traceback on one line above a
        # remediation rubric about making it faster. The gate still
        # fails closed — an unverified breach is not a pass — but it
        # names what it knows and what it does not.
        terminalreporter.write_sep(
            "=",
            "BUDGET GATE — the isolation re-measure could not be performed",
            red=True,
        )
        terminalreporter.write_line(f"  reason: {isolated.unavailable_reason}", red=True)
        if isolated.detail:
            terminalreporter.write_line(isolated.detail, red=True)
        terminalreporter.write_line("")
        for in_suite, nodeid in breaches:
            terminalreporter.write_line(f"  {in_suite:6.3f}s in suite (unverified)  {nodeid}")
        terminalreporter.write_line("")
        terminalreporter.write_line(
            "  This is NOT a timing verdict. The re-run above is the cause —",
            yellow=True,
        )
        terminalreporter.write_line(
            "  most often a test that errors in a fresh process (a broken",
            yellow=True,
        )
        terminalreporter.write_line(
            "  import, a fixture needing the network) rather than a slow one.",
            yellow=True,
        )
        terminalreporter.write_line(
            "  Fix that and re-run; the gate fails closed so the breach is not",
            yellow=True,
        )
        terminalreporter.write_line("  silently passed.", yellow=True)
        pytest.exit(
            f"\nthe budget gate could not verify {len(breaches)} in-suite breach(es): "
            f"{isolated.unavailable_reason}. Pre-commit rejects this commit.",
            returncode=1,
        )
    noise: list[tuple[float, str, float]] = []
    confirmed: list[tuple[float, str]] = []
    for in_suite, nodeid in breaches:
        fresh = isolated.durations.get(nodeid, in_suite)
        if fresh > HARD_LIMIT_S:
            confirmed.append((fresh, nodeid))
        else:
            noise.append((fresh, nodeid, in_suite))

    if not confirmed:
        terminalreporter.write_sep("=", "BUDGET GATE — in-suite noise, not cost", yellow=True)
        for fresh, nodeid, in_suite in noise:
            terminalreporter.write_line(
                f"  {in_suite:6.3f}s in suite → {fresh:6.3f}s isolated  {nodeid}", yellow=True
            )
        terminalreporter.write_line(
            "\n  All in-suite breaches cleared the limit on re-measure. Run passes.",
            yellow=True,
        )
        return

    terminalreporter.write_sep(
        "=",
        f"HARD LIMIT VIOLATIONS (>= {HARD_LIMIT_S:.2f}s, confirmed in isolation)",
        red=True,
    )
    for duration, nodeid in confirmed:
        terminalreporter.write_line(f"  {duration:6.3f}s  {nodeid}", red=True)
    if noise:
        for fresh, nodeid, in_suite in noise:
            terminalreporter.write_line(
                f"  {in_suite:6.3f}s → {fresh:6.3f}s isolated (cleared)  {nodeid}", yellow=True
            )
    terminalreporter.write_line("")
    terminalreporter.write_line("Remediation rubric — apply in order:", yellow=True)
    terminalreporter.write_line(
        "  1. DELETE  if it tests something we don't own",
        yellow=True,
    )
    terminalreporter.write_line(
        "  2. MOVE    to tests/integration/ if it touches external services",
        yellow=True,
    )
    terminalreporter.write_line(
        "     (real git subprocess, qmd daemon, separate interpreter)",
        yellow=True,
    )
    terminalreporter.write_line(
        "  3. MOCK    for isolated unit tests",
        yellow=True,
    )
    terminalreporter.write_line(
        "     (stub qmd helpers, atomic_commit, snapshot envelope)",
        yellow=True,
    )
    terminalreporter.write_line(
        "  4. COMPRESS (lower timeouts/hold_s, smaller fixtures, lazy stubs)",
        yellow=True,
    )
    terminalreporter.write_line(
        "  5. MARK @pytest.mark.slow if none of the above fit",
        yellow=True,
    )
    pytest.exit(
        f"\n{len(confirmed)} unit test(s) exceeded the {HARD_LIMIT_S:.2f}s hard limit "
        "when measured in isolation; see remediation rubric above. Pre-commit rejects "
        "this commit.",
        returncode=1,
    )
