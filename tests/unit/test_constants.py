"""Pin the package-wide constants to prevent silent rename drift."""

from __future__ import annotations

from lies.constants import LIES_DATA_SUBDIR


def test_lies_data_subdir_value() -> None:
    """The XDG subdir name must stay ``lies`` until a coordinated
    migration rewrites every wiki's data_root, errors message,
    cli/mcp/migrate path layouts, and tests in lockstep."""
    assert LIES_DATA_SUBDIR == "lies"
