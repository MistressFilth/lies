"""Thin wrapper around the `qmd` CLI for batch operations.

Use this for: `qmd update`, `qmd status`, `qmd collection add/remove`,
`qmd ls`, `qmd query`. For agent-native search, use the MCP client
(`qmd/mcp.py`).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Real `qmd query --format json` returns each hit's `file` field as
# ``qmd://<collection>/<path-within-collection>``. Downstream consumers
# (synthesizer, memory retrieval) consume a flat ``path`` key, so we strip
# the ``qmd://<collection>/`` prefix once at this boundary. The contract
# documented in :func:`qmd_query` ("Each result is a dict with at least a
# ``path`` key") is what every caller depends on.
_QMD_URI_PREFIX_RE = re.compile(r"^qmd://[^/]+/")
_QMD_URI_PREFIX_PREFIX = "qmd://"


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


def qmd_collection_add_if_missing(cwd: Path, path: Path, name: str) -> None:
    """Register ``name`` with qmd, treating "already exists" as success.

    Idempotent. Lets callers re-run a sync without raising on the second
    pass. Any other non-zero exit (real qmd error) still propagates so
    the pipeline can react.
    """
    result = _run(["collection", "add", str(path), "--name", name], cwd=cwd)
    if result.returncode == 0:
        return
    stderr = result.stderr.strip()
    if "already exists" in stderr.lower():
        return
    raise QmdError(f"qmd collection add failed: {stderr}")


def qmd_ls(cwd: Path, collection: str) -> str:
    """List files in a qmd collection."""
    result = _run(["ls", collection], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd ls failed: {result.stderr.strip()}")
    return str(result.stdout)


def is_qmd_installed() -> bool:
    """Return True if `qmd` is on PATH."""
    return shutil.which("qmd") is not None


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

    return [_normalize_qmd_result(item) for item in data]


def _normalize_qmd_result(item: Any) -> dict[str, Any]:
    """Normalize one qmd hit into the internal ``path`` contract.

    The real qmd CLI emits each hit with ``file: "qmd://<collection>/<path>"``
    and no top-level ``path`` key. We strip the ``qmd://<collection>/``
    prefix into ``path`` so downstream consumers (synthesizer,
    :func:`lies.memory.retrieval.search_wiki`) can rely on the contract
    documented in :func:`qmd_query`. If qmd already emitted a ``path`` key,
    leave it alone (the caller's explicit value wins).
    """
    if not isinstance(item, dict):
        return {"path": ""}
    result = dict(item)
    if "path" in result and isinstance(result["path"], str):
        return result
    file_value = result.get("file")
    if isinstance(file_value, str) and file_value.startswith(_QMD_URI_PREFIX_PREFIX):
        result["path"] = _QMD_URI_PREFIX_RE.sub("", file_value, count=1)
    else:
        # No usable file field; synthesize an empty path so consumers can
        # drop the hit cleanly instead of crashing on missing keys.
        result["path"] = ""
    return result
