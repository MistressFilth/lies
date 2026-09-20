"""Subprocess seam for qmd library functions.

Tests mock ``_proc.run`` instead of ``subprocess.run`` directly. This
isolates shell-out behavior and matches the Bundle E E.2 boundary
discipline used by the mcp/scraper/library modules.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def run(args: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess[Any]:
    """Run a qmd subprocess and return the CompletedProcess.

    The caller decides what to do with a non-zero ``returncode``;
    ``_proc.run`` does not raise. Tests mock this function.
    """
    return subprocess.run(
        ["qmd", *args],
        cwd=cwd,
        timeout=timeout,
        check=False,
        capture_output=True,
        text=True,
    )
