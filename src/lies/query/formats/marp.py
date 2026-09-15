"""Marp render: side-effect to marp CLI; markdown fallback on failure.

The function never raises on subprocess failure; the operator always
gets a path they can open. Stderr from a failed marp call is preserved
as a sidecar at <body_path>.stderr for inspection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

# Monotonic counter that disambiguates two render_marp calls landing in
# the same wall-clock second. The counter is process-local; it does not
# coordinate across processes (callers run in a single CLI process).
_UNIQ_COUNTER = count(1)


def _default_output_dir() -> Path:
    """Default cache dir for marp output: ${XDG_CACHE_HOME:-~/.cache}/lies/."""
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return cache / "lies"


def _timestamp() -> str:
    """ISO-8601 UTC timestamp with `:` replaced by `-` (filesystem-safe)."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace(":", "-")


def render_marp(body: str, *, output_dir: Path | None = None) -> Path:
    """Render a Marp body. Returns a path the operator should open.

    If ``marp`` is on ``$PATH``: shell out to render HTML at
    ``<output_dir>/query-<timestamp>.html``; the body is also written
    to ``<output_dir>/query-<timestamp>.md``. Returns the HTML path.

    If ``marp`` is missing OR the subprocess fails: write the body to
    ``<output_dir>/query-<timestamp>.md`` and return that path. On
    subprocess failure the marp stderr is captured at
    ``<body_path>.stderr`` for inspection.

    The marp CLI's arg order (``marp --html --output OUT IN``) is the
    documented form. If a future marp release changes this, update the
    ``subprocess.run`` call accordingly.
    """
    target_dir = output_dir if output_dir is not None else _default_output_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    # Wall-clock seconds + a process-local counter keeps two calls in the
    # same second from colliding on the same filename.
    stamp = f"{_timestamp()}-{next(_UNIQ_COUNTER)}"
    body_path = target_dir / f"query-{stamp}.md"
    html_path = target_dir / f"query-{stamp}.html"
    body_path.write_text(body, encoding="utf-8")

    if shutil.which("marp") is None:
        return body_path

    try:
        subprocess.run(
            ["marp", "--html", "--output", str(html_path), str(body_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return html_path
    except subprocess.CalledProcessError as exc:
        # Capture stderr to sidecar; return the markdown path so the
        # operator still has the body to read.
        stderr_path = Path(str(body_path) + ".stderr")
        stderr_text = exc.stderr if exc.stderr else f"marp exit {exc.returncode}"
        stderr_path.write_text(stderr_text, encoding="utf-8")
        return body_path


__all__ = ("render_marp",)
