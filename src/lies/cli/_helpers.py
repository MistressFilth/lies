"""Shared internal helpers used by multiple CLI group modules.

Re-exports names that the rest of the package (and tests) reach through
``import lies.cli as cli_module`` -- keep that public surface stable.

Heavy imports that would otherwise need to live at the top of a group
module (e.g. ``lies.orchestrator``, ``lies.providers``) are deliberately
NOT re-exported here. Each group module imports what its own commands
need inside the command bodies, so ``import lies.cli`` stays cheap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from lies.constants import LIES_DATA_SUBDIR
from lies.lock_errors import WikiFlockUnrepairable, WikiLockBusy
from lies.utils.exclusive import MAX_FLOCK_AGE_S, acquire_create_lock
from lies.utils.lock_heartbeat import read_heartbeat, read_owner_pid
from lies.utils.logging import configure_logging

# Note: WikiLinkCorpusMissing / WikiLinkResolver are intentionally NOT
# re-exported here. They are exposed via the PEP 562 `__getattr__` shim
# in cli/__init__.py (see _LAZY_ATTRS) so they load lazily on first
# attribute access -- keeping `import lies.cli` cheap.

__all__ = (
    "LIES_DATA_SUBDIR",
    "MAX_FLOCK_AGE_S",
    "_HINT",
    "WikiFlockUnrepairable",
    "WikiLockBusy",
    "_emit_missing_providers_hint",
    "_stdout_isatty",
    "acquire_create_lock",
    "configure_logging",
    "read_heartbeat",
    "read_owner_pid",
)


_HINT = (
    "providers.toml not found at {path}. "
    "Run `lies providers init` to bootstrap, or write the file by hand."
)


# Module-level seams so tests can simulate TTY / non-TTY cleanly without
# wrestling with click's CliRunner stream replacement (which always
# swaps `sys.stdout` for a BytesIO-backed `_NamedTextIOWrapper` and is
# immune to attribute patching on the C-level `isatty` method).
def _stdout_isatty() -> bool:
    """Whether stdout is attached to a TTY.

    Wrapped so tests can monkeypatch this rather than fighting the click
    stream wrapper's C-level isatty.
    """
    try:
        return sys.stdout.isatty()
    except AttributeError, ValueError:
        return False


def _emit_missing_providers_hint(path: Path) -> None:
    """Print a one-shot bootstrap pointer to stderr when providers.toml
    is missing and we're attached to a TTY.

    Skipped silently under CI / pipes so logs stay clean.

    NOTE: ``_stdout_isatty`` is looked up at call time via ``lies.cli``
    (not via the import-time module-level binding) so tests that
    ``monkeypatch.setattr(lies.cli, "_stdout_isatty", ...)`` take effect.
    """
    import lies.cli as _cli

    if path.exists():
        return
    if not _cli._stdout_isatty():
        return
    typer.echo(_HINT.format(path=path), err=True)
