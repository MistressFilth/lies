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

from pathlib import Path

import pytest

HARD_LIMIT_S = 0.15


# Pre-import ``lies.qmd.capability`` so the per-test ``monkeypatch.setattr``
# on its attributes doesn't pay the ~2s module-load cost on the first
# fixture invocation. The module imports ``pydantic_ai`` + ``fastmcp``
# transitively, which is heavy; amortising the cost at conftest load time
# keeps every test's autouse stub fast.
import lies.qmd.capability  # noqa: E402, F401  # pre-import for monkeypatch.setattr cost

# Pre-import ``lies.cli`` so the first CLI test in the session doesn't pay
# the ~200ms Typer import cost (which the hard-limit gate would otherwise
# attribute to whichever test happens to be first in collection order).
import lies.cli  # noqa: E402, F401  # pre-import for CLI test first-call cost

# Pre-import modules whose first-call cost (transitive pydantic_ai /
# fastmcp imports) is otherwise attributed to whichever test happens to
# be first in collection order. These imports are pure side-effects of
# keeping the hard-limit gate's per-test measurement honest.
import lies.memory.models  # noqa: E402, F401
import lies.wiki.registry  # noqa: E402, F401
import lies.wiki.registry_errors  # noqa: E402, F401
import lies.providers  # noqa: E402, F401
import lies.memory.service  # noqa: E402, F401
import lies.agents.linter  # noqa: E402, F401
import lies.agents.page_writer  # noqa: E402, F401
import lies.agents.query_synthesizer  # noqa: E402, F401
import lies.agents.source_reader  # noqa: E402, F401
import lies.agents.collection_author  # noqa: E402, F401
import lies.agents.repair  # noqa: E402, F401


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


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runslow",
        action="store_true",
        default=False,
        help="run tests marked as slow (default: skip them)",
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


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Fail the run when any non-slow-marked test exceeded the hard limit.

    The pre-commit ``test`` hook (which invokes ``make unit-test``)
    inherits the failure, so a commit with a regression test that
    breaches the 0.15s budget is rejected with a printed remediation
    rubric. Slow-marked tests are exempt: they run only with
    ``--runslow`` and are explicitly opt-in to higher cost.
    """
    violations = sorted(
        (
            (duration, nodeid)
            for nodeid, (duration, is_slow) in _call_durations.items()
            if duration > HARD_LIMIT_S and not is_slow
        ),
        key=lambda pair: -pair[0],
    )
    if not violations:
        return
    terminalreporter.write_sep(
        "=",
        f"HARD LIMIT VIOLATIONS (>= {HARD_LIMIT_S:.2f}s)",
        red=True,
    )
    for duration, nodeid in violations:
        terminalreporter.write_line(f"  {duration:6.3f}s  {nodeid}", red=True)
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
        f"\n{len(violations)} unit test(s) exceeded the {HARD_LIMIT_S:.2f}s hard limit; "
        "see remediation rubric above. Pre-commit rejects this commit.",
        returncode=1,
    )
