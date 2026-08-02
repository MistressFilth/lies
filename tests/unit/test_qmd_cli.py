from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lies.qmd.cli import (
    QmdError,
    QmdNotInstalledError,
    qmd_collection_add,
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
