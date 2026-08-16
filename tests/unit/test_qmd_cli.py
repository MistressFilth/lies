from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lies.qmd.cli import (
    QmdError,
    QmdNoResultsError,
    QmdNotInstalledError,
    qmd_collection_add,
    qmd_query,
    qmd_status,
    qmd_update,
)


def test_qmd_update_success(tmp_path: Path) -> None:
    with (
        patch("lies.qmd.cli.shutil.which", return_value="/usr/bin/qmd"),
        patch("lies.qmd.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        qmd_update(tmp_path)
        mock_run.assert_called_once()
        args = mock_run.call_args.args[0]
        assert args[0] == "qmd"
        assert args[1] == "update"


def test_qmd_status_returns_stdout(tmp_path: Path) -> None:
    with (
        patch("lies.qmd.cli.shutil.which", return_value="/usr/bin/qmd"),
        patch("lies.qmd.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok\n", stderr=""
        )
        result = qmd_status(tmp_path)
        assert result == "ok\n"


def test_qmd_not_installed(tmp_path: Path) -> None:
    with patch("lies.qmd.cli.shutil.which", return_value=None), pytest.raises(QmdNotInstalledError):
        qmd_update(tmp_path)


def test_qmd_error(tmp_path: Path) -> None:
    with (
        patch("lies.qmd.cli.shutil.which", return_value="/usr/bin/qmd"),
        patch("lies.qmd.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="some error"
        )
        with pytest.raises(QmdError, match="some error"):
            qmd_update(tmp_path)


def test_qmd_collection_add(tmp_path: Path) -> None:
    with (
        patch("lies.qmd.cli.shutil.which", return_value="/usr/bin/qmd"),
        patch("lies.qmd.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        qmd_collection_add(tmp_path, tmp_path / "wiki", "mywiki")
        args = mock_run.call_args.args[0]
        assert args[:3] == ["qmd", "collection", "add"]
        assert "mywiki" in args


# ---------------------------------------------------------------------------
# qmd_query: JSON output shape normalization
# ---------------------------------------------------------------------------
#
# The real `qmd query ... --format json` returns each result as
# ``{"docid": ..., "score": ..., "file": "qmd://<collection>/<path>",
#   "title": ..., "snippet": ...}`` — there is no ``path`` key. The
# downstream synthesizer and memory retrieval both consume ``path`` (the
# legacy internal contract), so :func:`qmd_query` must normalize the
# ``file`` field into a ``path`` key stripped of the ``qmd://<collection>/``
# prefix. The bug fixed by P3-b: ``_qmd_search_dispatch`` was reading
# ``r["path"]`` against a payload that only had ``r["file"]``, so
# ``qmd_paths`` was always empty, ``_QmdNoResults`` fired, and the
# synthesizer fell back to ``wiki/index.md`` even when qmd returned hits.


def _qmd_json_payload() -> str:
    """The exact JSON shape returned by ``qmd query --format json``."""
    return json.dumps(
        [
            {
                "docid": "#22b4ff",
                "score": 0.88,
                "file": "qmd://mywiki/chunk-0068.md",
                "line": 14,
                "title": "What is a hook?",
                "snippet": "excerpt...",
            },
            {
                "docid": "#abcd12",
                "score": 0.42,
                "file": "qmd://mywiki/entities/postgres.md",
                "line": 3,
                "title": "Postgres",
                "snippet": "excerpt...",
            },
        ]
    )


def test_qmd_query_normalizes_file_to_path(tmp_path: Path) -> None:
    """Each result gains a ``path`` key stripped of the qmd:// prefix."""
    with (
        patch("lies.qmd.cli.shutil.which", return_value="/usr/bin/qmd"),
        patch("lies.qmd.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=_qmd_json_payload(),
            stderr="",
        )
        results = qmd_query(tmp_path, "What is a hook?", limit=5)
        assert len(results) == 2
        assert results[0]["path"] == "chunk-0068.md"
        assert results[1]["path"] == "entities/postgres.md"
        # Original `file` field is preserved (callers may want it).
        assert results[0]["file"] == "qmd://mywiki/chunk-0068.md"
        # Other keys pass through unchanged.
        assert results[0]["docid"] == "#22b4ff"
        assert results[0]["score"] == 0.88


def test_qmd_query_preserves_path_when_already_present(tmp_path: Path) -> None:
    """If qmd already emitted a ``path`` key, do not overwrite it."""
    payload = json.dumps(
        [
            {
                "docid": "#1",
                "score": 0.5,
                "path": "wiki/entities/postgres.md",
                "file": "qmd://mywiki/entities/postgres.md",
            }
        ]
    )
    with (
        patch("lies.qmd.cli.shutil.which", return_value="/usr/bin/qmd"),
        patch("lies.qmd.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )
        results = qmd_query(tmp_path, "q", limit=5)
        assert results[0]["path"] == "wiki/entities/postgres.md"


def test_qmd_query_handles_missing_file_field(tmp_path: Path) -> None:
    """Rows without a ``file`` field get an empty ``path`` and survive."""
    payload = json.dumps([{"docid": "#x", "score": 0.1, "title": "Orphan"}])
    with (
        patch("lies.qmd.cli.shutil.which", return_value="/usr/bin/qmd"),
        patch("lies.qmd.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )
        results = qmd_query(tmp_path, "q", limit=5)
        assert len(results) == 1
        # No file field → path defaults to "" so the synthesizer drops it.
        assert results[0].get("path", "") == ""


def test_qmd_query_empty_list_still_raises_no_results(tmp_path: Path) -> None:
    """Sanity: the empty-list branch is unaffected by the normalization."""
    with (
        patch("lies.qmd.cli.shutil.which", return_value="/usr/bin/qmd"),
        patch("lies.qmd.cli.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )
        with pytest.raises(QmdNoResultsError):
            qmd_query(tmp_path, "nothing", limit=5)
