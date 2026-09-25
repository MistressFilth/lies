"""Tests for ``qmd_query`` in :mod:`lies.qmd.cli`.

Task 2 of the qmd-drain Spec A plan. The previous
``subprocess.run(capture_output=True, text=True, ...)`` shape was
deadlock-prone on long stderr writes (~30 KB Node.js stack traces
on VRAM OOM, which approaches the 64 KB OS pipe buffer on some
platforms). The new ``_run_qmd`` helper in :mod:`lies.qmd._subprocess`
replaces that path with Popen + ``communicate(timeout=...)`` and a
DEVNULL stdin, plus an 8 KB stderr truncation cap. The test below
exercises the integration so a future regression in
``qmd_query``'s subprocess plumbing is caught at the call site,
not just inside the helper.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from lies.qmd.cli import QmdError, qmd_query


@pytest.mark.slow
def test_qmd_query_does_not_deadlock_on_long_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qmd_query must not deadlock when qmd emits a long stderr trace.

    Mocks the qmd binary path so we can spawn a Python script that
    emits 100 KB to stderr then sleeps. The new ``_run_qmd`` path
    uses Popen + ``communicate(timeout=...)`` which drains both
    pipes concurrently and bounds stderr to 8 KB; the test asserts
    the call returns within 3 s regardless of stderr volume.

    Note on Python's ``subprocess.run(capture_output=True, ...)``:
    that helper *also* uses threads to drain pipes, so on modern
    Python (3.6+) it does not deadlock on a 100 KB stderr write
    either. The hard claim "OLD code deadlocks" from the brief's
    TDD step does not hold in practice; the test still passes
    against the OLD code. Its value is as a regression guard: if
    a future change in ``qmd_query`` reintroduces a pipe-full
    stall (e.g. switching to a non-reading Popen or dropping the
    stderr reader), this test fails at the call site.

    The brief's original draft used ``time.sleep(0.5)`` in the
    fake qmd; compressed to 0.05 s to fit the 0.15 s wall-clock
    budget enforced by the pre-commit unit-test gate. The
    deadlock contract (parent reads the long stderr without
    hanging) is independent of the post-write hold time. The
    ``dt < 3.0`` budget gives plenty of headroom for the spawn +
    100 KB-stderr-write + 50 ms-sleep cycle.
    """
    fake_qmd = tmp_path / "qmd"
    fake_qmd.write_text(
        "#!/bin/sh\n"
        f"{sys.executable} -c \"import sys; sys.stderr.write('x' * 100000); "
        'sys.stderr.flush(); import time; time.sleep(0.05)"\n'
    )
    fake_qmd.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    t0 = time.monotonic()
    try:
        qmd_query(
            cwd=tmp_path,
            question="any",
            limit=5,
            timeout=5.0,
        )
    except QmdError:
        # The fake qmd exits cleanly with no stdout, so qmd_query
        # raises QmdNoResultsError (not QmdCommandError). Any
        # QmdError subclass is acceptable here — what matters is
        # that the call returns promptly rather than hanging.
        pass
    dt = time.monotonic() - t0
    assert dt < 3.0, f"qmd_query took {dt:.2f}s; expected < 3s"
