"""The sidecar override's import-time binding, which a rebind cannot reach."""

# ``SIDECAR_PATH`` binds at import, so an in-process rebind cannot exercise the
# host hazard: the variable has to be exported *before* the module is first
# imported. That needs a fresh interpreter, which the unit budget's own rubric
# routes here ("separate interpreter"). The rebind half is
# ``tests/unit/qmd/test_sidecar_override.py``.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_READ = "from lies.qmd.daemon import read_sidecar_scope; print(read_sidecar_scope())"


def _interpreter(override: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _READ],
        capture_output=True,
        text=True,
        env={**os.environ, "LIES_QMD_SIDECAR_OVERRIDE": str(override)},
    )


def test_a_directory_override_set_before_import_names_the_variable(tmp_path: Path) -> None:
    """The directory case, bound by a real import rather than a rebind."""
    proc = _interpreter(tmp_path)
    assert proc.returncode != 0, proc.stdout
    assert "LIES_QMD_SIDECAR_OVERRIDE" in proc.stderr
    assert "file path" in proc.stderr


def test_a_file_override_set_before_import_is_accepted(tmp_path: Path) -> None:
    """The valid case, bound by a real import rather than a rebind."""
    target = tmp_path / "mcp.data-dir"
    target.write_text("seeded", encoding="utf-8")
    proc = _interpreter(target)
    assert proc.returncode == 0, proc.stderr
    assert "seeded" in proc.stdout
