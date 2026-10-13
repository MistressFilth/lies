"""File system harness capability, scoped to the wiki root."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def file_system(wiki_root: Path) -> Any:
    """Return a file system capability bounded to ``wiki_root``.

    ``root_dir`` is the *guardrail*, not the substrate. Since
    pydantic-ai-harness 0.54.0 every ``FileSystem`` operation resolves
    through the run's workspace: ``FileSystem.before_run`` calls
    ``require_workspace``, and a run with no workspace attached fails at
    its start, before the model is ever called. Constructing this
    capability is therefore only half the configuration.
    """
    from pydantic_ai_harness.filesystem import FileSystem

    return FileSystem(root_dir=wiki_root)


def local_workspace(wiki_root: Path) -> Any:
    """Return the ``LocalWorkspace`` capability that supplies a run its workspace."""
    from pydantic_ai.capabilities import LocalWorkspace

    return LocalWorkspace(working_dir=str(wiki_root))
