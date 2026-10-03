"""Deadlock-free subprocess helper for qmd CLI invocations.

The previous implementation in ``lies.qmd.cli.qmd_query`` used
``subprocess.run(capture_output=True, ...)``, which opens a pipe for
stderr. When qmd emits a long Node.js stack trace (~30+ KB on VRAM
OOM), the child blocks writing stderr because the OS pipe buffer
(~64 KB) fills; the parent's timeout cannot fire.

Replaces that pattern: ``stdin=DEVNULL``, piped stdout/stderr,
SIGKILL on the whole process group on timeout, stderr truncated to
``_MAX_STDERR_BYTES``.

**Do NOT strip ``start_new_session=True``.** qmd is a bun-injected
bash shim that forks a node.js grandchild; without the flag the
grandchild survives ``proc.kill()`` (14 zombie grandchildren at 77-89%
CPU reproduced live). ``start_new_session=True`` puts every
descendant in one group rooted at the helper; ``os.killpg`` reaps
them all.
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

# Diagnostic cap on stderr returned to callers. Truncates the
# pathological Node.js stack traces VRAM OOM emits; bytes beyond the
# cap are dropped, not left in the pipe.
_MAX_STDERR_BYTES = 8 * 1024

# Post-kill drain window. Long enough for a child to react to SIGKILL
# and let the kernel reap it.
_DRAIN_TIMEOUT_S = 2.0

# Silence = wedge. qmd reports each phase ("Embedding 35 queries...",
# "Reranking 40 chunks..."); silence is evidence, duration is not.
DEFAULT_IDLE_TIMEOUT_S = 30.0

# Idle bound for commands silent for their whole duration (``qmd
# embed``, ``qmd update``). Below the total bound: an idle bound
# equal to the total can never fire. Cost: a wedged one holds
# ``with_qmd_lock()`` for half its total bound.
SILENT_COMMAND_IDLE_TIMEOUT_FRACTION = 0.5

_POLL_INTERVAL_S = 0.1


class QmdWedgeError(subprocess.TimeoutExpired):
    """A qmd subprocess was killed for going silent or overrunning.

    Subclass of the stdlib exception so existing
    ``except subprocess.TimeoutExpired`` keeps working. ``bound`` is
    ``"idle"`` or ``"total"``; ``last_output`` is the tail of stderr
    at the moment of the kill.
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

    ``NO_COLOR`` is forced on. Verified against qmd 2.5.3's source
    (``dist/cli/qmd.js:92``); LIES always pipes so colour is already
    off. Defence in depth: an operator who exported ``NO_COLOR=0``
    must not be able to change what LIES parses out of a subprocess.
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

    The idle clock is reset by every byte qmd writes; only a process
    that has gone quiet is killed. The absolute ceiling is the
    backstop.

    Args:
        args: argv list. First entry is the qmd binary path.
        cwd: working directory.
        timeout: seconds before SIGKILL.

    Returns:
        ``subprocess.CompletedProcess`` with stdout bytes and stderr
        bytes (truncated to ``_MAX_STDERR_BYTES``).

    Raises:
        subprocess.TimeoutExpired: whole process group SIGKILL'd.
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
            start_new_session=True,
        )
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
                    reader = out_stream if key.data == "out" else err_stream
                    assert reader is not None
                    # ``read1`` returns whatever is buffered without
                    # waiting to fill 64 KiB — the entire point of a
                    # progress reader. ``BufferedReader`` method, not on
                    # the ``IO`` protocol; the value is a real pipe.
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
                        # bytes left in the pipe would block the
                        # child, which is the deadlock this module
                        # exists to prevent.
                        captured.extend(chunk)
                        if on_output is not None:
                            on_output(chunk.decode("utf-8", errors="replace"))
        finally:
            sel.close()

        stdout_b = b"".join(out_chunks)
        stderr_b = bytes(captured)
        if bound is None:
            # Both pipes at EOF does not mean the child has been reaped.
            try:
                proc.wait(timeout=_DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError, PermissionError:
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
            # Kill the whole process group so descendants cannot outlive
            # the bound. ``killpg`` raises if the group is already gone.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError, PermissionError:
                pass
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
        # Normal-exit safety net: a long-running grandchild the child
        # forked must still be reaped.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            pass
        return subprocess.CompletedProcess(
            args=args,
            returncode=proc.returncode,
            stdout=stdout_b,
            stderr=stderr_b[:_MAX_STDERR_BYTES],
        )
    finally:
        os.close(stdin_fd)
