"""Wiki name validation."""

from __future__ import annotations

from lies.errors import WikiNameError

_MAX_NAME_LEN = 200


def validate_name(name: str) -> None:
    """Validate a wiki name. Raises WikiNameError on invalid."""
    if not name:
        raise WikiNameError(name, reason="empty")
    if name in (".", ".."):
        raise WikiNameError(name, reason="reserved")
    if "/" in name or "\\" in name:
        raise WikiNameError(name, reason="path separator")
    if "\x00" in name:
        raise WikiNameError(name, reason="null byte")
    if name.startswith("."):
        raise WikiNameError(name, reason="leading dot")
    if len(name) > _MAX_NAME_LEN:
        raise WikiNameError(name, reason=f"longer than {_MAX_NAME_LEN} chars")
