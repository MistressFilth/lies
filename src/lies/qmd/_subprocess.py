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
import selectors
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

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

# How long a qmd subprocess may go without emitting a single byte of
# stderr before it is considered wedged.
#
# This is the bound that separates "slow" from "hung", which an
# absolute deadline cannot do. qmd reports each phase as it works --
# ``Expanding query... (1ms)``, ``Embedding 35 queries... (2.6s)``,
# ``Reranking 40 chunks... (1ms)`` -- so silence is evidence and
# duration is not. Measured on the 5987-document corpus: the embedding
# step alone runs 2.6s, and a cold first call was 13.8s. A query that
# keeps talking is doing work and is never killed here, however long
# it runs; one that stops talking for this long is not going to
# finish, whatever the absolute ceiling says.
DEFAULT_IDLE_TIMEOUT_S = 30.0

# How often the reader loop wakes to check the two bounds. Short
# enough that the idle bound is honoured to within a fraction of a
# second, long enough not to spin a core.
_POLL_INTERVAL_S = 0.1


class QmdWedgeError(subprocess.TimeoutExpired):
    """A qmd subprocess was killed for going silent or overrunning.

    A subclass of the stdlib exception so every existing
    ``except subprocess.TimeoutExpired`` in the product keeps working
    unchanged -- the seam is the one thing callers already handle.

    ``bound`` records *which* limit fired, which is the whole point:

    ``"idle"``
        The process stopped emitting. This is the wedge signal, and it
        fires on its own schedule regardless of the total budget.
    ``"total"``
        The absolute ceiling. The process kept talking and never
        finished -- a backstop for work that progresses but does not
        converge, not evidence of a hang.

    ``last_output`` is the tail of stderr at the moment of the kill.
    It is the difference between "died mid-embedding" and "went quiet
    right after reranking started", which is the only thing that tells
    a reader whether to look at VRAM or at the index.
    """

    def __init__(
        self,
        cmd: list[str],
        bound: str,
        *,
        timeout: float,
        idle_timeout: float,
        last_output: str = "",
        stderr: bytes | None = None,
    ) -> None:
        super().__init__(cmd, timeout)
        self.bound = bound
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.last_output = last_output
        self.stderr = stderr

    def __str__(self) -> str:
        if self.bound == "idle":
            which = f"stopped emitting for {self.idle_timeout:g}s"
        else:
            which = f"exceeded the {self.timeout:g}s total budget"
        tail = f"; last output: {self.last_output!r}" if self.last_output else ""
        return f"qmd {which} (idle bound {self.idle_timeout:g}s){tail}"


def _child_env() -> dict[str, str]:
    """The environment every qmd subprocess runs with.

    ``NO_COLOR`` is forced on for the child, and that is the whole point
    of this function.

    qmd renders a spinner — ``⠋ Gathering information`` with cursor
    escapes — **on stdout**, while gating it on ``process.stderr.isTTY``.
    LIES always pipes both streams, so the gate is false but the write
    still happens, and the spinner lands in the middle of the JSON that
    ``qmd_query`` parses. The result is
    ``qmd query returned invalid JSON: Expecting value: line 1 column 1
    (char 0)`` — raised only when the query is slow enough for the
    spinner to render, which is why it looked intermittent.

    Verified directly against qmd 2.5.3: with the same command and the
    same fixture, default env puts the spinner on stdout and
    ``NO_COLOR=1`` puts clean JSON there. The spinner is also suppressed
    by ``CI=1`` and ``TERM=dumb``, but neither is under LIES' control —
    the child inherits whatever the operator's shell happens to export.

    Overriding rather than passing ``env=os.environ`` through: an
    operator who has *already* exported ``NO_COLOR=0`` to re-enable
    colour in their own terminal must not be able to reintroduce a
    spinner into a machine-parsed stream.

    Progress on stderr is unaffected — that is the wedge signal
    :mod:`lies.qmd._subprocess` exists to read, and it stays on.
    """
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    return env


def _run_qmd(
    args: list[str],
    cwd: Path,
    timeout: float,
    *,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_S,
    on_output: Callable[[str], None] | None = None,
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
            env=_child_env(),
            text=False,  # bytes mode — caller decodes explicitly
            # ``start_new_session=True`` puts the child (and every
            # descendant it forks, including qmd's node.js grandchild
            # via the bun shim) in a fresh process group rooted at
            # the helper. The timeout-kill path signals the whole
            # group via ``os.killpg``, not just the immediate child;
            # see the module docstring for the bug history.
            start_new_session=True,
        )
        # Two bounds, watched together. ``communicate(timeout=N)`` was
        # all-or-nothing: one absolute deadline cannot tell a slow query
        # from a wedged one, and the only answer it gave was "timed out"
        # -- true of both, and therefore useful for neither. Here the
        # idle clock is reset by every byte qmd writes, so a query that
        # keeps reporting progress survives no matter how long it takes,
        # and only a process that has genuinely gone quiet is killed.
        # The absolute ceiling remains as the backstop for work that
        # progresses forever without converging.
        started = time.monotonic()
        last_output_at = started
        captured = bytearray()
        out_chunks: list[bytes] = []
        bound: str | None = None

        sel = selectors.DefaultSelector()
        out_stream: IO[bytes] | None = proc.stdout
        err_stream: IO[bytes] | None = proc.stderr
        assert out_stream is not None
        assert err_stream is not None
        sel.register(out_stream, selectors.EVENT_READ, "out")
        sel.register(err_stream, selectors.EVENT_READ, "err")
        open_streams = 2
        try:
            while open_streams:
                now = time.monotonic()
                if now - started > timeout:
                    bound = "total"
                    break
                if now - last_output_at > idle_timeout:
                    bound = "idle"
                    break
                for key, _ in sel.select(_POLL_INTERVAL_S):
                    stream = key.fileobj
                    # ``selectors`` types ``fileobj`` as an fd or a
                    # HasFileno; both streams registered above are
                    # buffered readers, and reading through the
                    # selector key would have to be suppressed to
                    # typecheck. Re-binding the value the loop already
                    # knows is honest and needs no ignore.
                    reader = out_stream if key.data == "out" else err_stream
                    assert reader is not None
                    # ``read1`` returns whatever is already buffered
                    # without waiting to fill 64 KiB, which is the
                    # entire point of a progress reader -- the idle
                    # clock has to see bytes as they arrive, not in
                    # 64 KiB quanta. It is a ``BufferedReader``
                    # method, so it is absent from the ``IO``
                    # protocol this local is declared against; the
                    # value is a real pipe from ``Popen(stdout=PIPE)``
                    # and always a buffered reader in practice.
                    chunk = reader.read1(65536)  # ty: ignore[unresolved-attribute]
                    if not chunk:
                        sel.unregister(stream)
                        open_streams -= 1
                        continue
                    last_output_at = time.monotonic()
                    if key.data == "out":
                        out_chunks.append(chunk)
                    else:
                        # Keep draining even past the capture cap:
                        # bytes left sitting in the pipe would block
                        # the child on write, which is the very
                        # deadlock this module exists to prevent.
                        captured.extend(chunk)
                        if on_output is not None:
                            on_output(chunk.decode("utf-8", errors="replace"))
        finally:
            sel.close()

        stdout_b = b"".join(out_chunks)
        stderr_b = bytes(captured)
        if bound is None:
            # Both pipes at EOF does not mean the child has been
            # reaped -- only that it closed its streams. Without this
            # the CompletedProcess below carries ``returncode=None``
            # and every caller sees a successful run as a mystery.
            try:
                proc.wait(timeout=_DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                # A child that closed stdout/stderr but will not exit
                # is the same class of problem as one that says
                # nothing: it is not making progress. Fall through to
                # the total-bound path rather than reporting success.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=_DRAIN_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    pass
                raise QmdWedgeError(
                    args,
                    "total",
                    timeout=timeout,
                    idle_timeout=idle_timeout,
                    last_output=(
                        stderr_b.decode("utf-8", errors="replace").strip().splitlines() or [""]
                    )[-1],
                    stderr=stderr_b,
                ) from None
        if bound is not None:
            # Kill the entire process group so descendants (e.g. qmd's
            # node grandchild forked by the bun shim) cannot outlive
            # the bound that fired. ``killpg`` raises if the group is
            # already empty (race with natural exit before we got
            # here); swallow that.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            # Drain post-kill so the kernel can reap the child.
            try:
                proc.communicate(timeout=_DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                pass
            lines_seen = stderr_b.decode("utf-8", errors="replace").strip().splitlines()
            raise QmdWedgeError(
                args,
                bound,
                timeout=timeout,
                idle_timeout=idle_timeout,
                last_output=lines_seen[-1] if lines_seen else "",
                stderr=stderr_b,
            )
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
