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
and SIGKILLs the entire process group on timeout before
propagating. Stderr is truncated to ``_MAX_STDERR_BYTES`` so a
runaway subprocess cannot return a multi-megabyte trace.

The ask project solved the same problem years ago in
``repo/ask/scripts/qmd-daemon.py:213-219`` with ``DEVNULL``
explicitly. LIES diverged; this fix restores the pattern.

Process-group note (do NOT strip ``start_new_session=True``):
on this host, ``qmd`` is a bun-injected bash shim that forks a
node.js grandchild and exec's into it. ``subprocess.Popen``
without ``start_new_session=True`` puts the immediate child in
the parent's process group, but the grandchild lands in a fresh
group after the fork+exec and survives a plain ``proc.kill()``.
Live verification reproduced 14 zombie grandchildren at 77-89%
CPU when the SIGKILL-only path was exercised; that is the exact
hang scenario Spec A was supposed to fix. ``start_new_session=True``
puts every descendant in one process group rooted at the helper,
and ``os.killpg`` on timeout reaps them all. Reverting the flag
will reintroduce the leak.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

# Diagnostic cap on stderr returned to callers. Larger than any
# reasonable qmd error message (~2 KB in practice); truncates the
# pathological Node.js stack traces that VRAM OOM and similar
# failures emit. Captured bytes beyond the cap are dropped, not
# left in the pipe (would block child on write).
_MAX_STDERR_BYTES = 8 * 1024

# Post-kill drain window. Long enough for a child to react to
# SIGKILL and let the kernel reap it; short enough that a wedged
# child cannot stall the parent for more than a couple of seconds
# on its way out the door.
_DRAIN_TIMEOUT_S = 2.0


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
            ``timeout``. The entire process group is SIGKILL'd so
            descendants (qmd's node.js grandchild when invoked via
            the bun shim) cannot outlive the parent's deadline.
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
            # ``start_new_session=True`` puts the child (and every
            # descendant it forks, including qmd's node.js grandchild
            # via the bun shim) in a fresh process group rooted at
            # the helper. The timeout-kill path signals the whole
            # group via ``os.killpg``, not just the immediate child;
            # see the module docstring for the bug history.
            start_new_session=True,
        )
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the entire process group so descendants (e.g.
            # qmd's node grandchild forked by the bun shim) cannot
            # outlive the parent deadline. ``killpg`` raises if the
            # group is already empty (race with natural exit before
            # we got here, or a child that handles SIGKILL cleanly
            # and exits before we signal); swallow that.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            # Drain pipes post-kill so the kernel can reap the child.
            try:
                stdout_b, stderr_b = proc.communicate(timeout=_DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                stdout_b = b""
                stderr_b = b""
            raise
        # Normal-exit safety net: if the immediate child exited but
        # forked a long-running grandchild we still want to signal
        # the group empty. ``killpg`` is a no-op when only the helper
        # remains; ``ProcessLookupError`` is the documented signal
        # that the group is already gone.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return subprocess.CompletedProcess(
            args=args,
            returncode=proc.returncode,
            stdout=stdout_b,
            stderr=stderr_b[:_MAX_STDERR_BYTES],
        )
    finally:
        os.close(stdin_fd)
