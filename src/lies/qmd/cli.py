"""Thin wrapper around the `qmd` CLI for batch operations.

Use this for: `qmd update`, `qmd status`, `qmd collection add/remove`,
`qmd ls`, `qmd query`. For agent-native search, use the MCP client
(`qmd/mcp.py`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class QmdError(Exception):
    """Base class for qmd-related failures."""


class QmdNotInstalledError(QmdError):
    """Raised when the `qmd` binary is not found on PATH."""


class QmdNoResultsError(QmdError):
    """Raised when `qmd query` returns an empty result set."""


class QmdCommandError(QmdError):
    """Raised when a `qmd query` exits non-zero or returns malformed output."""


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
    """Run ``qmd update`` in ``cwd``.

    ``qmd update`` reindexes every collection registered under ``cwd``;
    it has no per-collection flag (only ``--pull``). Callers that want
    a per-collection refresh must filter at the qmd config layer, not
    via this CLI.
    """
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


def is_qmd_installed() -> bool:
    """Return True if `qmd` is on PATH."""
    return shutil.which("qmd") is not None


def qmd_embed(cwd: Path, *, force: bool = False) -> None:
    """Re-run the embedding model on existing chunks.

    Not yet implemented; the upstream qmd CLI exposes no ``embed``
    subcommand, so this is a placeholder. Callers should treat it as
    a no-op until the embed/cleanup stages land.
    """
    return


def qmd_cleanup(cwd: Path) -> None:
    """Remove orphan chunks not referenced by any collection.

    Not yet implemented; placeholder. See ``qmd_embed``.
    """
    return


def qmd_query(
    cwd: Path,
    question: str,
    limit: int = 5,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """Run `qmd query` and return parsed JSON results.

    Each result is a dict with at least a ``path`` key (the wiki-relative
    path of the matching page). The synthesizer only consumes ``path``;
    additional keys are preserved for callers that need scores/snippets.

    Raises:
        QmdNotInstalledError: If `qmd` is not on PATH.
        QmdCommandError: If the qmd command exits non-zero or returns
            malformed output.
        QmdNoResultsError: If qmd returns an empty result list.
    """
    if not is_qmd_installed():
        raise QmdNotInstalledError("`qmd` not found on PATH")

    try:
        result = subprocess.run(
            ["qmd", "query", question, "--limit", str(limit), "--json"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise QmdNotInstalledError("`qmd` binary not found at exec time") from exc
    except subprocess.TimeoutExpired as exc:
        raise QmdCommandError(f"qmd query timed out after {timeout}s") from exc

    if result.returncode != 0:
        raise QmdCommandError(
            f"qmd query failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise QmdNoResultsError(f"qmd query returned no results for: {question!r}")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise QmdCommandError(f"qmd query returned invalid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise QmdCommandError(f"qmd query expected a JSON list, got {type(data).__name__}")

    if not data:
        raise QmdNoResultsError(f"qmd query returned no results for: {question!r}")

    return data
