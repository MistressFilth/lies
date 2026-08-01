from __future__ import annotations

"""Long-running pandoc subprocess with persistent stdin.

Each ``convert()`` call writes its input to the daemon's stdin and
closes stdin (signaling end-of-input to pandoc), then reads stdout
fully. Pandoc flushes the converted output and waits for the next
input. The daemon process is reused across calls. Restarts on crash.
"""
import subprocess
import threading
import time

_IDLE_TIMEOUT_S = 60


class PandocDaemon:
    def __init__(self, idle_timeout_s: int = _IDLE_TIMEOUT_S) -> None:
        self._idle_timeout_s = idle_timeout_s
        self._proc: subprocess.Popen[bytes] | None = None
        self._last_used: float = 0.0
        self._lock = threading.Lock()

    def _start(self) -> None:
        self._proc = subprocess.Popen(
            ["pandoc", "--from=html", "--to=gfm", "--wrap=none"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def convert(self, input_bytes: bytes, from_format: str) -> bytes:
        """Write input, close stdin (flush), read stdout fully.

        On crash (post-read ``poll()`` returns non-None), restart the
        daemon and retry once. Returns the last successful answer.
        """
        with self._lock:
            out = b""
            for _attempt in range(2):
                if self._proc is None or self._proc.poll() is not None:
                    self._start()
                assert self._proc is not None
                assert self._proc.stdin is not None and self._proc.stdout is not None
                self._proc.stdin.write(input_bytes)
                self._proc.stdin.flush()
                self._proc.stdin.close()
                out = bytes(self._proc.stdout.read())
                self._last_used = time.monotonic()
                if self._proc.poll() is None:
                    return out
            return out

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def shutdown(self) -> None:
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
