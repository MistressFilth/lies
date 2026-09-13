"""Integration test: concurrent subprocess race resolution.

Spawns 3 concurrent Python subprocesses that each call into
``src.lies.qmd.lock``'s decorator. Verifies:

1. All subprocesses return successfully (no OOM, no deadlock).
2. Total wall-clock is at least 3 times a single-subprocess hold
   (the lock serializes; they do not run truly in parallel).

Mirrors the production path: every ``qmd_*`` CLI helper is wrapped
in :func:`lies.qmd.lock.with_qmd_lock`, and the decorator's
``_acquire_with_poll`` does the cross-process flock serialization.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_three_concurrent_subprocesses_serialize_through_qmd_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three concurrent Python subprocesses that each acquire the qmd lock
    must serialize — total wall-clock at least 3 times the single-process
    hold time.
    """
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(tmp_path / "qmd.lock"))

    inline_script = textwrap.dedent(f"""\
        import sys
        import time
        import random
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from lies.qmd.lock import with_qmd_lock

        @with_qmd_lock(timeout_s=30.0, max_age_s=1800.0)
        def hold() -> str:
            time.sleep(0.1 + 0.05 * random.random())
            return "ok"

        print(hold())
    """)

    script_path = tmp_path / "script.py"
    script_path.write_text(inline_script, encoding="utf-8")

    started = time.monotonic()
    procs = [
        subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(3)
    ]
    outs = [p.communicate(timeout=60) for p in procs]
    elapsed = time.monotonic() - started

    for i, (out, err) in enumerate(outs):
        assert out.decode().strip() == "ok", f"subprocess {i} stderr: {err.decode()}"
        assert procs[i].returncode == 0

    assert elapsed >= 0.3, f"expected serialization, got {elapsed:.2f}s"
