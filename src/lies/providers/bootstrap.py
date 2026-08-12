"""Wizard orchestration + atomic writer for providers.toml.

``write_atomic`` is the single IO helper every write path funnels
through (wizard + ``ops.py`` companion subcommands). Crash-mid-write
recovery inherits from the sibling-tmp + ``os.replace`` + ``fsync``
pattern that ``Registry.save`` and ``save_collection`` already use.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from lies.providers.config import ProvidersConfig, ProviderSpec

log = logging.getLogger(__name__)


class BootstrapAborted(Exception):
    """Wizard cancelled by the operator (^C, blank answer, invalid input)."""


class BootstrapValidationFailed(BootstrapAborted):
    """Multiple accumulated input errors; wizard will re-prompt or abort."""


class ProvidersConfigMissing(Exception):
    """Companion subcommand invoked but providers.toml does not exist.

    Distinct from the wizard's 'file-exists' guard (which lives in
    ``cli.py`` and exits 2). Companion commands raise this to suggest
    the operator run ``lies providers init`` first.
    """


@dataclass
class PartialConfig:
    providers: dict[str, ProviderSpec]
    default_model: str | None
    agents: dict[str, str]


PromptFn = Callable[[str, str], str]


def write_atomic(target_path: os.PathLike[str], partial: PartialConfig) -> None:
    """Commit ``partial`` to ``target_path`` atomically.

    Raises ``OSError`` on filesystem failure. Never leaves a partial
    file behind because ``os.replace`` is atomic on POSIX.
    """
    from lies.providers.editor import to_toml

    cfg = ProvidersConfig(
        providers=partial.providers,
        default_model=partial.default_model or "anthropic:claude-opus-4-7",
        agents=partial.agents,
    )
    target = os.fspath(target_path)
    payload = to_toml(cfg)
    directory = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(prefix=".providers.toml.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
