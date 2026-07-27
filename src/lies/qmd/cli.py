"""Thin wrapper around the `qmd` CLI for batch operations.

Use this for: `qmd update`, `qmd status`, `qmd collection add/remove`,
`qmd ls`. For agent-native search, use the MCP client (`qmd/mcp.py`).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


class QmdNotInstalledError(Exception):
    """Raised when the `qmd` binary is not found on PATH."""


class QmdError(Exception):
    """Raised when a `qmd` command exits non-zero."""


def _run(args: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess[Any]:
    """Run a qmd command, raising on failure."""
    if shutil.which("qmd") is None:
        raise QmdNotInstalledError("`qmd` not found on PATH. Install: npm i -g @tobilu/qmd")
    try:
        return subprocess.run(
            ["qmd", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise QmdNotInstalledError("`qmd` not found on PATH") from exc


def qmd_update(cwd: Path) -> None:
    """Reindex the qmd collections under `cwd`."""
    result = _run(["update"], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd update failed: {result.stderr.strip()}")


def qmd_status(cwd: Path) -> str:
    """Return qmd's status output for the collections under `cwd`."""
    result = _run(["status"], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd status failed: {result.stderr.strip()}")
    return str(result.stdout)


def qmd_collection_add(cwd: Path, path: Path, name: str) -> None:
    """Register a collection with qmd."""
    result = _run(["collection", "add", str(path), "--name", name], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd collection add failed: {result.stderr.strip()}")


def qmd_ls(cwd: Path, collection: str) -> str:
    """List files in a qmd collection."""
    result = _run(["ls", collection], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd ls failed: {result.stderr.strip()}")
    return str(result.stdout)