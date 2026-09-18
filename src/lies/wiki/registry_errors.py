"""Wiki registry persistence errors."""

from __future__ import annotations

from pathlib import Path


class RegistryCorrupt(Exception):
    """A registry file failed JSON parsing or schema validation."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"registry at {path} is corrupt: {reason}")
        self.path = path
        self.reason = reason


class RegistryVersionUnsupported(Exception):
    """The registry file's ``version`` field is not supported."""

    def __init__(self, path: Path, found: int, supported: int) -> None:
        super().__init__(
            f"registry at {path} has version {found}, this build supports up to {supported}"
        )
        self.path = path
        self.found = found
        self.supported = supported


class RegistryWriteFailed(Exception):
    """Atomic write of the registry failed."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"failed to write registry at {path}: {reason}")
        self.path = path
        self.reason = reason
