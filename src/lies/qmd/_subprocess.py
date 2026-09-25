"""Deadlock-free subprocess helper for qmd CLI invocations.

The previous implementation in :func:`lies.qmd.cli.qmd_query` used
``subprocess.run(capture_output=True, ...)``, which opens a pipe
for stderr. When qmd emits a long Node.js stack trace (typically
30+ KB on VRAM OOM), the child blocks writing stderr because the
OS pipe buffer (~64 KB) fills. The parent's
``subprocess.run(timeout=5)`` then waits forever: the child is
alive but stalled, and the timeout cannot fire.

This module replaces that pattern. The subprocess is launched
with ``stdin=DEVNULL`` (caller never writes), ``stdout=PIPE``,
``stderr=PIPE``. The helper reads via ``communicate(timeout=...)``
and SIGKILLs the child on timeout before propagating. Stderr is
truncated to ``_MAX_STDERR_BYTES`` so a runaway subprocess cannot
return a multi-megabyte trace.

The ask project solved the same problem years ago in
``repo/ask/scripts/qmd-daemon.py:213-219`` with ``DEVNULL``
explicitly. LIES diverged; this fix restores the pattern.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Diagnostic cap on stderr returned to callers. Larger than any
# reasonable qmd error message (~2 KB in practice); truncates the
# pathological Node.js stack traces that VRAM OOM and similar
# failures emit. Captured bytes beyond the cap are dropped, not
# left in the pipe (would block child on write).
_MAX_STDERR_BYTES = 8 * 1024


def _run_qmd(
    args: list[str],
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Run a qmd subprocess with DEVNULL stdin + bounded stderr capture.

    Args:
        args: argv list. First entry is the qmd binary path.
        cwd: working directory.
        timeout: seconds before SIGKILL.

    Returns:
        ``subprocess.CompletedProcess`` with stdout bytes (untruncated)
        and stderr bytes (truncated to ``_MAX_STDERR_BYTES``). Both are
        bytes — callers decode explicitly because the wrapper enforces
        a single encoding contract (UTF-8).

    Raises:
        subprocess.TimeoutExpired: when the subprocess exceeds
            ``timeout``. The child is SIGKILL'd before propagating so
            no zombie / hung process is left behind.
        FileNotFoundError: when ``args[0]`` does not exist.
    """
    stdin_fd = os.open(os.devnull, os.O_RDONLY)
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdin=stdin_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=None,
            text=False,  # bytes mode — caller decodes explicitly
        )
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            # Drain pipes post-kill so the child can actually exit.
            try:
                proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass  # child is wedged; kernel will reap eventually
            raise
        return subprocess.CompletedProcess(
            args=args,
            returncode=proc.returncode,
            stdout=stdout_b,
            stderr=stderr_b[:_MAX_STDERR_BYTES],
        )
    finally:
        os.close(stdin_fd)
