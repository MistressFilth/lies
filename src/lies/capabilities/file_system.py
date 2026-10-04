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
    """Return the ``LocalWorkspace`` capability that supplies a run its workspace.

    This is the other half of :func:`file_system`, and it belongs on the
    agent's ``capabilities`` list rather than on any individual run: the
    harness documents it as ``Agent(..., capabilities=[LocalWorkspace(path)])``,
    and putting it there means every run is covered — including one added
    later. ``Agent.__init__`` takes no workspace and ``run_sync`` rejects a
    ``LocalWorkspace`` (it is a capability, not a backend), so the
    capability list is the wiring the API actually offers.

    Rooted at ``wiki_root`` so it matches the capability's guardrail: the
    model can only address paths inside the boundary, and the workspace
    those paths resolve against is that same boundary.
    """
    from pydantic_ai.capabilities import LocalWorkspace

    return LocalWorkspace(working_dir=str(wiki_root))
