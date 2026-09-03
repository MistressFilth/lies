from __future__ import annotations

"""Single-shot pandoc conversion wrapper.

Pandoc uses EOF to delimit one document on stdin and does not provide a
length-prefixed framing protocol for multiple documents. Each ``convert``
call therefore starts a fresh subprocess, sends one document with
``communicate()``, and waits for that process to exit. The ``PandocDaemon``
name and ``idle_timeout_s`` argument remain for API compatibility; there is
no persistent process or idle timeout. A failed process is retried once.
"""

import subprocess  # noqa: E402
import threading  # noqa: E402


class PandocDaemon:
    """Run one isolated pandoc subprocess per conversion.

    This compatibility name is retained for callers of the original API,
    but conversions are intentionally single-shot because closing stdin is
    the only reliable input boundary supported by the pandoc CLI.
    """

    def __init__(self, idle_timeout_s: int = 60) -> None:
        # Keep the argument for compatibility with existing callers. A
        # single-shot process has no idle lifetime to manage.
        self._idle_timeout_s = idle_timeout_s
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _start(self, from_format: str) -> None:
        self._proc = subprocess.Popen(
            ["pandoc", f"--from={from_format}", "--to=gfm", "--wrap=none"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def convert(self, input_bytes: bytes, from_format: str) -> bytes:
        """Convert one document in a fresh subprocess, retrying once on failure."""
        with self._lock:
            out = b""
            for _attempt in range(2):
                self._start(from_format)
                assert self._proc is not None
                stdout, _stderr = self._proc.communicate(input=input_bytes)
                out = bytes(stdout)
                if self._proc.returncode == 0:
                    break
            self._proc = None
            return out

    def is_alive(self) -> bool:
        """Return whether a conversion subprocess is currently running."""
        return self._proc is not None and self._proc.poll() is None

    def shutdown(self) -> None:
        """Terminate an in-flight conversion, if one exists."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None

    def __enter__(self) -> PandocDaemon:  # noqa: PYI034 - use Self on Python 3.11+
        return self

    def __exit__(self, *args: object) -> None:
        self.shutdown()
