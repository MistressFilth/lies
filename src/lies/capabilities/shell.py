"""Shell harness capability, with a command allowlist."""
from __future__ import annotations

from typing import Any


def shell(allowlist: list[str], timeout: int = 60) -> Any:
    """Return a shell capability limited to the listed command basenames.

    The harness `Shell` capability rejects combining an `allowed_commands`
    allowlist with the default `denied_commands` denylist (mixing the two modes
    is unsupported). When a caller specifies an allowlist we therefore disable
    the destructive-command denylist; the caller takes full responsibility for
    what is allowed.
    """
    from pydantic_ai_harness.shell import Shell

    return Shell(
        allowed_commands=allowlist,
        denied_commands=[],
        default_timeout=timeout,
    )
