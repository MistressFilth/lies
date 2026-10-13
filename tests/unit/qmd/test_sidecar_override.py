"""``LIES_QMD_SIDECAR_OVERRIDE`` must name a file, and say so when it does not."""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.qmd import daemon as qmd_daemon
from lies.qmd.daemon import SidecarOverrideNotAFile


def test_a_directory_override_names_the_variable_not_the_file_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both accessors refuse a directory and name ``LIES_QMD_SIDECAR_OVERRIDE``."""
    monkeypatch.setattr(qmd_daemon, "SIDECAR_PATH", tmp_path)
    for call in (
        lambda: qmd_daemon.read_sidecar_scope(),
        lambda: qmd_daemon.write_sidecar_scope(tmp_path / "scope"),
    ):
        with pytest.raises(SidecarOverrideNotAFile) as excinfo:
            call()
        message = str(excinfo.value)
        assert "LIES_QMD_SIDECAR_OVERRIDE" in message
        assert str(tmp_path) in message
        assert "file path" in message


def test_a_valid_file_path_still_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The valid case is unaffected by the guard."""
    monkeypatch.setattr(qmd_daemon, "SIDECAR_PATH", tmp_path / "mcp.data-dir")
    qmd_daemon.write_sidecar_scope(tmp_path / "wiki")
    assert qmd_daemon.read_sidecar_scope() == tmp_path / "wiki"


def test_a_parent_that_does_not_exist_yet_still_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_dir`` is False for a not-yet-created path, so first run still works."""
    monkeypatch.setattr(qmd_daemon, "SIDECAR_PATH", tmp_path / "fresh" / "mcp.data-dir")
    qmd_daemon.write_sidecar_scope(tmp_path / "wiki")
    assert qmd_daemon.read_sidecar_scope() == tmp_path / "wiki"
