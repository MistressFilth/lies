"""Subprocess seam for qmd library functions.

Tests mock ``_proc.run`` instead of ``subprocess.run`` directly. This
isolates shell-out behavior and matches the Bundle E E.2 boundary
discipline used by the mcp/scraper/library modules.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from lies.qmd._subprocess import _run_qmd


def run(args: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess[Any]:
    """Run a qmd subprocess via the deadlock-free :func:`_run_qmd` helper.

    Spec A of the qmd-drain plan: every subprocess call in this module
    routes through ``_run_qmd`` so a runaway qmd stderr trace cannot
    block the parent on a full pipe buffer. ``_run_qmd`` returns
    ``CompletedProcess`` with bytes stdout/stderr; we decode to str
    so the F38 contract (``qmd_cleanup`` /
    :func:`lies.qmd.cli.qmd_reindex` reading ``result.stderr`` as
    text) is preserved.

    The caller decides what to do with a non-zero ``returncode``;
    ``_proc.run`` does not raise. Tests mock this function.
    """
    result = _run_qmd(["qmd", *args], cwd=cwd, timeout=timeout)
    return subprocess.CompletedProcess(
        args=result.args,
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8", errors="replace"),
        stderr=result.stderr.decode("utf-8", errors="replace"),
    )
