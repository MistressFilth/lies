"""Shell harness capability, with a command allowlist."""
from __future__ import annotations

from typing import Any


def shell(allowlist: list[str], timeout: int = 60) -> Any:
    """Return a shell capability limited to the listed command basenames."""
    from pydantic_ai_harness.shell import Shell

    return Shell(allowed_commands=allowlist, default_timeout=timeout)
