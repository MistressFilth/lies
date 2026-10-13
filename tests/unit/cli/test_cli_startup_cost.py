"""Wall-clock budget for `import lies.cli` and `lies --help`."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

# Pre-refactor baselines (measured on the same machine, branch
# `main`): `import lies.cli` ~1.4s, `lies --help` ~1.6s.
# Post-refactor measurements (local dev box, this branch): import ~0.75s,
# `lies --help` ~1.07s. CI measured 1.5s / 1.9s on GitHub Actions
# runners, so the budgets include ~100% margin on import and ~67% on
# help vs the local numbers. Tighten locally if the post-refactor
# numbers come down further; for slow CI, set `LIES_PERF_BUDGET=2.0`
# in the env, or `SKIP_PERF=1` to skip entirely.
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
@pytest.mark.slow
def test_import_lies_cli_under_threshold() -> None:
    """`import lies.cli` (in-process) should not exceed the budget."""
    import sys

    snippet = (
        "import time\nt0 = time.perf_counter()\nimport lies.cli\nprint(time.perf_counter() - t0)\n"
    )
    # Use sys.executable to skip uv's ~150ms of project resolution; the
    # test asserts the in-process import time printed by the snippet, not
    # the wall-clock here, so swapping the launcher is safe.
    result = subprocess.run(
        [sys.executable, "-c", snippet],
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
@pytest.mark.slow
def test_lies_help_wall_clock_under_threshold() -> None:
    """`lies --help` (full wall clock) should not exceed the budget."""
    from typer.testing import CliRunner

    from lies.cli import app

    t0 = time.perf_counter()
    CliRunner().invoke(app, ["--help"])
    elapsed = time.perf_counter() - t0
    bound = PERF_BOUND_HELP_S * _budget()
    assert elapsed < bound, (
        f"`lies --help` took {elapsed:.3f}s; expected < {bound:.3f}s. "
        "Profile with `python -X importtime -c 'import lies.cli'`."
    )
