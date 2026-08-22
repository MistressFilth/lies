"""Wall-clock budget for `import lies.cli` and `lies --help`.

These are soft perf gates. The numbers below are calibrated to a
moderately-loaded dev machine; CI may need to relax the bounds
further via the `LIES_PERF_BUDGET` env var (multiplier) or skip
the test entirely under `SKIP_PERF=1`.

Two thresholds, two different things:

- **`import lies.cli` in-process time** — measures the actual Python
  import. We do this by running `python -c "import time; t0=...;
  import lies.cli; print(time.perf_counter() - t0)"` in a subprocess
  and reading the printed value, so the measurement excludes
  `uv run` startup overhead. The lazy-cli-imports refactor targets
  this: it should drop meaningfully vs. the pre-refactor baseline.

- **`lies --help` wall-clock time** — measures the user-perceived
  latency of running the command. This INCLUDES the `uv run`
  interpreter spin-up (~400ms on this hardware), which is outside
  the refactor's reach. The budget is set above the import-time
  budget for that reason.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

# Pre-refactor baselines (measured on the same machine, branch
# `main`): `import lies.cli` ~1.4s, `lies --help` ~1.6s.
# Post-refactor targets: <2.0s for import, <2.5s for help.
# (Help budget absorbs the ~400ms `uv run` overhead. Both
# budgets include ~50% margin over the local dev box measurement
# of ~1.0s / ~1.5s so the test passes on slower CI hardware; tighten
# locally only if the post-refactor numbers come down further.)
# For slow CI, set `LIES_PERF_BUDGET=2.0` in the env, or `SKIP_PERF=1`
# to skip entirely.
PERF_BOUND_IMPORT_S = 2.0
PERF_BOUND_HELP_S = 2.5


def _budget() -> float:
    """Multiplier for the perf budgets, overridable via env for slow CI."""
    raw = os.environ.get("LIES_PERF_BUDGET", "1.0")
    try:
        return float(raw)
    except ValueError:
        return 1.0


@pytest.mark.skipif(os.environ.get("SKIP_PERF") == "1", reason="perf test skipped")
def test_import_lies_cli_under_threshold() -> None:
    """`import lies.cli` (in-process) should not exceed the budget."""
    snippet = (
        "import time\nt0 = time.perf_counter()\nimport lies.cli\nprint(time.perf_counter() - t0)\n"
    )
    result = subprocess.run(
        ["uv", "run", "python", "-c", snippet],
        capture_output=True,
        text=True,
        check=True,
    )
    elapsed = float(result.stdout.strip())
    bound = PERF_BOUND_IMPORT_S * _budget()
    assert elapsed < bound, (
        f"`import lies.cli` took {elapsed:.3f}s; expected < {bound:.3f}s. "
        "Profile with `python -X importtime -c 'import lies.cli'`."
    )


@pytest.mark.skipif(os.environ.get("SKIP_PERF") == "1", reason="perf test skipped")
def test_lies_help_wall_clock_under_threshold() -> None:
    """`lies --help` (full wall clock) should not exceed the budget."""
    t0 = time.perf_counter()
    subprocess.run(
        ["uv", "run", "lies", "--help"],
        capture_output=True,
        check=True,
    )
    elapsed = time.perf_counter() - t0
    bound = PERF_BOUND_HELP_S * _budget()
    assert elapsed < bound, (
        f"`lies --help` took {elapsed:.3f}s; expected < {bound:.3f}s. "
        "Note: ~400ms is `uv run` interpreter spin-up, outside the refactor's reach. "
        "Profile with `python -X importtime -c 'import lies.cli'`."
    )
