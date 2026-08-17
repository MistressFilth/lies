"""Wiki-related exception types."""

from __future__ import annotations

from pathlib import Path

from lies.constants import LIES_DATA_SUBDIR

# Re-exported here so callers that import ``WikiLockBusy`` from the
# ``lies.errors`` namespace continue to work after the class moved to
# :mod:`lies.lock_errors`. The canonical definition lives there now and
# subclasses :class:`lies.lock_errors.WikiFlockError`.
from lies.lock_errors import WikiLockBusy

__all__ = ["WikiAlreadyExists", "WikiLockBusy", "WikiNameError", "WikiNotRegistered"]


class WikiAlreadyExists(Exception):
    """Raised when ``lies init <name>`` targets a name already registered."""

    def __init__(self, name: str, existing_path: Path) -> None:
        self.name = name
        self.existing_path = existing_path
        super().__init__(
            f"wiki '{name}' already exists at {existing_path}; "
            f"choose a different name or remove the existing one first."
        )


class WikiNotRegistered(Exception):
    """Raised when a non-init command targets an unregistered wiki name."""

    def __init__(self, name: str, data_home: Path) -> None:
        self.name = name
        self.data_home = data_home
        super().__init__(
            f"wiki '{name}' not registered at {data_home}/{LIES_DATA_SUBDIR}/{name}; "
            f"run 'lies init {name}' first or set LIES_WIKI_NAME."
        )


class WikiNameError(Exception):
    """Raised when a wiki name fails validation."""

    def __init__(self, name: str, *, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"invalid wiki name '{name}': {reason}")
