"""Thin wrapper around the `qmd` CLI for batch operations.

Use this for: `qmd update`, `qmd status`, `qmd collection add/remove`,
`qmd ls`, `qmd query`. For agent-native search, use the MCP client
(`qmd/mcp.py`).
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lies.qmd import _proc
from lies.qmd._models import ReindexResult
from lies.qmd._subprocess import (
    DEFAULT_IDLE_TIMEOUT_S,
    SILENT_COMMAND_IDLE_TIMEOUT_FRACTION,
    _run_qmd,
)
from lies.qmd.lock import with_qmd_lock


# Real `qmd query --format json` returns each hit's `file` field as
# ``qmd://<collection>/<path>``. Strip the prefix once at this boundary;
# keep the ``<collection>/`` segment (wiki pages live at
# ``wiki.wiki_dir/<collection>/<page>``).
def _is_default_root(env_var: str, lies_var: str, spec_default: str) -> bool:
    """True when this XDG root resolves to its spec default.

    Both spellings count, and ``LIES_XDG_*`` is read first by
    :mod:`lies.xdg` -- a guard inspecting only ``XDG_*`` would pass a
    caller who sandboxed through the LIES-prefixed variable, which is
    the spelling this repository documents.
    """
    for key in (lies_var, env_var):
        value = os.environ.get(key)
        if value:
            return Path(value).expanduser() == Path(spec_default).expanduser()
    return True


def _assert_qmd_index_isolated() -> None:
    """Refuse a qmd write when the library is sandboxed but the index is not.

    Only the write path is guarded. A sandboxed library may still read
    the live index -- the qmd daemon is machine-global and serves it
    to every client on the machine, so guarding reads would break the
    read path for the one configuration that is actually safe.
    """
    from lies.xdg import _LIES_OVERRIDE, _SPEC_DEFAULTS

    data_default = _is_default_root(
        "XDG_DATA_HOME", _LIES_OVERRIDE["XDG_DATA_HOME"], _SPEC_DEFAULTS["XDG_DATA_HOME"]
    )
    cache_default = _is_default_root(
        "XDG_CACHE_HOME", _LIES_OVERRIDE["XDG_CACHE_HOME"], _SPEC_DEFAULTS["XDG_CACHE_HOME"]
    )
    # The dangerous combination is a sandboxed library (data NOT at
    # its default) against a live index (cache AT its default).
    # Either half the other way round shares nothing.
    if data_default or not cache_default:
        return

    from lies.xdg import cache_home

    data_value = os.environ.get("XDG_DATA_HOME") or os.environ.get("LIES_XDG_DATA_HOME")
    raise QmdIndexIsolationError(
        "refusing to write the qmd index: the library is sandboxed "
        f"(data root {data_value}) but the qmd index is the live one "
        f"({cache_home() / 'qmd' / 'index.sqlite'}). qmd resolves its index "
        "from XDG_CACHE_HOME, not XDG_DATA_HOME, so redirecting the data root "
        "alone sandboxes the mirror files and still registers the collections "
        "in the shared index. Set XDG_CACHE_HOME (or LIES_XDG_CACHE_HOME) to "
        "the same sandbox to isolate the index, or unset the data redirect to "
        "write the real library."
    )


def _guards_qmd_index(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Refuse the write when the library is sandboxed and the index is not.

    Applied *above* ``@with_qmd_lock()`` so the refusal costs nothing
    and the host-wide lock is never taken just to say no.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _assert_qmd_index_isolated()
        return fn(*args, **kwargs)

    return wrapper


_QMD_URI_PREFIX_RE = re.compile(r"^qmd://")
_QMD_URI_PREFIX_PREFIX = "qmd://"


class QmdError(Exception):
    """Base class for qmd-related failures."""


class QmdNotInstalledError(QmdError):
    """Raised when the `qmd` binary is not found on PATH."""


class QmdNoResultsError(QmdError):
    """Raised when `qmd query` returns an empty result set."""


class QmdIndexIsolationError(QmdError):
    """A qmd write would land in the live index from a sandboxed library.

    qmd resolves its index from ``$XDG_CACHE_HOME``, and that index is
    one file shared by every process on the machine. Redirecting the
    library root alone therefore sandboxes the mirror files and
    nothing else. Measured::

        $ XDG_DATA_HOME=/tmp/sandbox qmd --help | grep ^Index:
        Index: /home/<user>/.cache/qmd/index.sqlite

    A refusal rather than better advice, because the combination has
    no legitimate caller: a throwaway library writing rows into an
    index every other collection shares.
    """


class QmdCommandError(QmdError):
    """Raised when a `qmd query` exits non-zero or returns malformed output."""


class QmdTimeoutError(QmdCommandError):
    """Raised when a `qmd` subprocess outlives its deadline.

    A timeout is its own type — qmd was running and had not finished,
    not rejecting work. Collapsing the two made ``search`` report a
    slow daemon as ``qmd unreachable``. ``stderr`` carries qmd's own
    output before the deadline — the only evidence of where the time
    went. ``None`` when the post-kill drain also timed out.
    """

    def __init__(self, message: str, stderr: bytes | str | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr


def _run(
    args: list[str],
    cwd: Path,
    timeout: int = 300,
    *,
    idle_timeout: float | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run a qmd command via the deadlock-free :func:`_run_qmd` helper.

    ``idle_timeout`` overrides the wedge detector's silence bound
    (default 30s). Some qmd commands are silent for their whole
    duration (``qmd embed`` writes a single spinner escape;
    ``qmd update`` writes nothing, gated on ``isTTY``). The silence
    is the operation, not a symptom — widening the bound keeps
    healthy progress from being killed.

    Callers that pass it derive it from their own ``timeout``; see
    :data:`SILENT_COMMAND_IDLE_TIMEOUT_FRACTION` for why it must
    stay strictly below the total.

    Raises:
        QmdNotInstalledError: ``qmd`` is not on PATH at exec time.
    """
    if shutil.which("qmd") is None:
        raise QmdNotInstalledError("`qmd` not found on PATH. Install: npm i -g @tobilu/qmd")
    try:
        result = _run_qmd(
            ["qmd", *args],
            cwd=cwd,
            timeout=timeout,
            idle_timeout=DEFAULT_IDLE_TIMEOUT_S if idle_timeout is None else idle_timeout,
        )
    except FileNotFoundError as exc:
        raise QmdNotInstalledError("`qmd` not found on PATH") from exc
    return subprocess.CompletedProcess(
        args=result.args,
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8", errors="replace"),
        stderr=result.stderr.decode("utf-8", errors="replace"),
    )


@_guards_qmd_index
@with_qmd_lock()
def qmd_update(cwd: Path, timeout: int = 1800) -> None:
    """Run ``qmd update`` in ``cwd``.

    The total bound is 1800s — ``qmd update`` reindexes every
    collection registered under ``cwd``. The idle bound is raised
    because under a pipe ``qmd update`` emits nothing (its progress
    is a stderr write gated on ``isTTY``). Cost: a wedged update
    holds ``with_qmd_lock()`` for up to 900s rather than 30s.
    """
    result = _run(
        ["update"],
        cwd=cwd,
        timeout=timeout,
        idle_timeout=timeout * SILENT_COMMAND_IDLE_TIMEOUT_FRACTION,
    )
    if result.returncode != 0:
        raise QmdError(f"qmd update failed: {result.stderr.strip()}")


@with_qmd_lock()
def qmd_status(cwd: Path) -> str:
    """Return qmd's status output for the collections under `cwd`."""
    result = _run(["status"], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd status failed: {result.stderr.strip()}")
    return str(result.stdout)


@_guards_qmd_index
@with_qmd_lock()
def qmd_collection_add(cwd: Path, path: Path, name: str) -> None:
    """Register a collection with qmd."""
    result = _run(["collection", "add", str(path), "--name", name], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd collection add failed: {result.stderr.strip()}")


@_guards_qmd_index
@with_qmd_lock()
def qmd_collection_add_if_missing(cwd: Path, path: Path, name: str) -> None:
    """Register ``name`` with qmd, treating "already exists" as success.

    Idempotent. Real qmd errors still propagate.
    """
    result = _run(["collection", "add", str(path), "--name", name], cwd=cwd)
    if result.returncode == 0:
        return
    stderr = result.stderr.strip()
    if "already exists" in stderr.lower():
        return
    raise QmdError(f"qmd collection add failed: {stderr}")


@_guards_qmd_index
@with_qmd_lock()
def qmd_collection_remove(cwd: Path, name: str) -> None:
    """Run ``qmd collection remove <name>`` in ``cwd``."""
    result = _run(["collection", "remove", name], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd collection remove failed: {result.stderr.strip()}")


@with_qmd_lock()
def qmd_collection_show(cwd: Path, name: str) -> dict[str, str] | None:
    """Return parsed ``qmd collection show <name>`` output, or None if missing.

    Non-zero exit (collection missing, qmd error) returns None instead
    of raising so the caller can branch on "register vs refresh".
    """
    result = _run(["collection", "show", name], cwd=cwd)
    if result.returncode != 0:
        return None
    info: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.startswith("  "):
            continue
        try:
            key, _, value = line.strip().partition(":")
        except ValueError:
            continue
        info[key.strip().lower()] = value.strip()
    if not info or "path" not in info:
        return None
    return {"path": info["path"]}


@_guards_qmd_index
@with_qmd_lock()
def qmd_collection_add_or_update(
    cwd: Path,
    path: Path,
    name: str,
    *,
    library_target: Path | None = None,
) -> None:
    """Register ``name`` at ``path`` with qmd, refreshing an existing entry.

    Missing → ``qmd collection add``. Same path → no-op. Different path
    → ``qmd collection remove`` then ``qmd collection add``; ``remove``
    failures are logged and the ``add`` proceeds.

    When ``library_target`` is set (library writer integration), that
    path is what gets registered.
    """
    register_path = library_target if library_target is not None else path
    target = str(register_path.resolve())
    info = qmd_collection_show(cwd, name)
    if info is None:
        qmd_collection_add(cwd, register_path, name)
        return
    existing = info.get("path", "")
    if existing == target:
        return
    result = _run(["collection", "remove", name], cwd=cwd)
    if result.returncode != 0:
        print(
            f"warning: qmd collection remove {name} failed: {result.stderr.strip()}; "
            f"continuing with add.",
            file=sys.stderr,
        )
    qmd_collection_add(cwd, register_path, name)


@_guards_qmd_index
@with_qmd_lock()
def qmd_embed(cwd: Path, collection_name: str, *, timeout: int = 1800) -> None:
    """Run ``qmd embed -c <collection_name>`` in ``cwd``.

    Default 30 minutes (embedding a large wiki on first ingest can be
    slow). The idle bound is raised to
    ``timeout * SILENT_COMMAND_IDLE_TIMEOUT_FRACTION`` — half the
    total, because the reader loop checks the total bound first and
    an idle bound equal to the total can never fire.

    Embedding is silent for its whole duration (under a pipe ``qmd
    embed`` writes a single spinner escape and then nothing while
    the model runs). Measured 9.4s of unbroken silence on a cold
    cache.

    Cost: a wedged embed holds ``with_qmd_lock()`` for up to half its
    total bound instead of 30s.
    """
    result = _run(
        ["embed", "-c", collection_name],
        cwd=cwd,
        timeout=timeout,
        idle_timeout=timeout * SILENT_COMMAND_IDLE_TIMEOUT_FRACTION,
    )
    if result.returncode != 0:
        raise QmdError(f"qmd embed failed: {result.stderr.strip()}")


@with_qmd_lock()
def qmd_ls(cwd: Path, collection: str) -> str:
    """List files in a qmd collection."""
    result = _run(["ls", collection], cwd=cwd)
    if result.returncode != 0:
        raise QmdError(f"qmd ls failed: {result.stderr.strip()}")
    return str(result.stdout)


@_guards_qmd_index
@with_qmd_lock()
def qmd_cleanup(cwd: Path) -> None:
    """Drop orphan rows from qmd's FTS5 db."""
    result = _proc.run(["cleanup"], cwd=cwd)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            returncode=result.returncode,
            cmd=result.args,
            output=result.stdout,
            stderr=result.stderr,
        )


@_guards_qmd_index
@with_qmd_lock()
def qmd_reindex(
    cwd: Path,
    *,
    embed: bool = False,
    cleanup: bool = False,
    all_: bool = False,
    force: bool = False,
) -> ReindexResult:
    """Restore the qmd index; gate destructive stages.

    Returns ``ReindexResult`` with the stages that ran. Failures are
    collected as ``errors`` rather than raised; the caller decides
    whether to surface them. Subsequent stages are skipped on prior
    failure so the envelope doesn't accumulate cascading errors.
    """
    result = ReindexResult()
    errors: list[str] = []

    if cleanup or all_:
        cleanup_result = _proc.run(["cleanup"], cwd=cwd)
        if cleanup_result.returncode != 0:
            errors.append(f"cleanup: {cleanup_result.stderr.strip()}")
        else:
            result.cleaned = True

    if force:
        cache_dir = cwd / ".qmd" / "cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir)

    if not errors:
        update_result = _proc.run(["update"], cwd=cwd)
        if update_result.returncode != 0:
            errors.append(f"update: {update_result.stderr.strip()}")
        else:
            result.indexed = True

    if (all_ or embed) and not errors:
        embed_result = _proc.run(["embed"], cwd=cwd, timeout=1800)
        if embed_result.returncode != 0:
            errors.append(f"embed: {embed_result.stderr.strip()}")
        else:
            result.embedded = True

    result.errors = errors
    return result


def is_qmd_installed() -> bool:
    """Return True if `qmd` is on PATH."""
    return shutil.which("qmd") is not None


def _parse_json_list(stdout_text: str) -> list[Any] | None:
    """Parse qmd's JSON list from stdout, tolerating a progress prefix.

    When qmd's model cache is cold, ``ipull`` (a transitive dependency
    of ``node-llama-cpp``) writes progress to stdout through
    ``stdout-update`` with no TTY guard. LIES pipes, so progress lands
    in front of the JSON and ``json.loads`` fails at char 0 — on
    exactly the runs where the cache was cold.

    Find the JSON array rather than demanding the stream begin with
    one. Scanning for ``[``/``{`` is safe (the prefix contains
    neither); the parse itself validates the payload. Keeps scanning
    past a JSON object and uses ``raw_decode`` (O(n) overall).
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(stdout_text):
        if char not in "[{":
            continue
        try:
            parsed, _ = decoder.raw_decode(stdout_text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return parsed
    return None


@with_qmd_lock()
def qmd_query(
    cwd: Path,
    question: str,
    limit: int = 5,
    timeout: int = 60,
    *,
    collection_filter: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Run `qmd query` and return parsed JSON results.

    Each result has at least a ``path`` key.

    ``collection_filter`` is applied post-qmd even though the CLI
    accepts ``-c, --collection <name>`` (verified against
    ``qmd query --help``; an earlier version of this docstring
    claimed the flag did not exist). The post-filter is kept for a
    reason that has nothing to do with availability: qmd applies its
    own row cap before LIES sees anything, so a filter applied inside
    qmd can still be starved when the rows it drops consumed the
    budget. Filtering after the fact cannot recover a row qmd never
    returned, which is why retrieval goes to the daemon's
    ``collections`` push-down instead -- and why a scoped CLI query
    remains bounded by qmd's own cap.

    ``limit`` is inert in qmd's CLI (``dist/cli/qmd.js:2550`` reads
    only ``values.n``); the slice is applied *after* the filter so a
    scoped caller gets rows the filter kept.

    Raises:
        QmdNotInstalledError: If `qmd` is not on PATH.
        QmdCommandError: If qmd exits non-zero or returns malformed output.
        QmdTimeoutError: If qmd outlives ``timeout``.
        QmdNoResultsError: If qmd returns an empty result list (after
            the post-filter is applied).
    """
    if not is_qmd_installed():
        raise QmdNotInstalledError("`qmd` not found on PATH")

    try:
        result = _run_qmd(
            ["qmd", "query", question, "--limit", str(limit), "--json"],
            cwd=cwd,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise QmdNotInstalledError("`qmd` binary not found at exec time") from exc
    except subprocess.TimeoutExpired as exc:
        raise QmdTimeoutError(
            f"qmd query timed out after {timeout}s",
            stderr=getattr(exc, "stderr", None),
        ) from exc

    if result.returncode != 0:
        stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
        raise QmdCommandError(f"qmd query failed (exit {result.returncode}): {stderr_text}")

    stdout_text = result.stdout.decode("utf-8", errors="replace").strip()
    if not stdout_text:
        raise QmdNoResultsError(f"qmd query returned no results for: {question!r}")

    data = _parse_json_list(stdout_text)
    if data is None:
        raise QmdCommandError(
            f"qmd query returned invalid JSON; first 200 chars of stdout: {stdout_text[:200]!r}"
        )

    if not data:
        raise QmdNoResultsError(f"qmd query returned no results for: {question!r}")

    normalized = [_normalize_qmd_result(item) for item in data]
    if collection_filter is None:
        return normalized[:limit]
    allowed = collection_filter
    filtered: list[dict[str, Any]] = []
    for hit in normalized:
        path = hit.get("path", "")
        first_segment = path.split("/", 1)[0] if path else ""
        if first_segment and first_segment in allowed:
            filtered.append(hit)
    if not filtered:
        raise QmdNoResultsError(
            f"qmd query returned no results matching collection filter "
            f"{sorted(allowed)!r} for: {question!r}"
        )
    return filtered[:limit]


def _normalize_qmd_result(item: Any) -> dict[str, Any]:
    """Normalize one qmd hit into the internal ``path`` contract.

    The real qmd CLI emits each hit with ``file:
    "qmd://<collection>/<path>"`` and no top-level ``path`` key. We
    strip just the ``qmd://`` prefix into ``path`` — the
    ``<collection>/`` segment is preserved (PR #39 layout).
    Downstream consumers (synthesizer, source-aware ``wiki_read``)
    join ``path`` onto ``wiki.wiki_dir``. If qmd already emitted a
    ``path`` key, leave it alone.
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
        warnings.warn(
            "qmd hit lacks a `qmd://` URI; `path` defaults to empty and the "
            "row will be dropped by downstream consumers.",
            UserWarning,
            stacklevel=2,
        )
        result["path"] = ""
    return result


# ``qmd_get`` was deleted here. It shelled out to ``qmd get`` for the
# librarian's source-aware read, which the seam moved to the daemon's
# ``get`` with ``lineNumbers: false``: the CLI line-numbers every line
# unconditionally and ``--no-line-numbers`` still leaves a
# ``qmd://path  #docid`` header, so the F19 citation contract
# ``[[slug]]: "verbatim quote"`` could not be met through it. The
# function had no remaining caller, and its docstring still named the
# dispatch that no longer existed. It also raised ``QmdCommandError``
# on a timeout where its sibling in this module raised
# ``QmdTimeoutError`` carrying qmd's stderr -- an asymmetry inside one
# file, which is the kind of thing the next caller copies. Use
# ``access.daemon_tool("get", {"file": path, "lineNumbers": False})``.
