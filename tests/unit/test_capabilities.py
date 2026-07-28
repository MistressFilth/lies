from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent

from lies.capabilities.code_mode import code_mode
from lies.capabilities.dynamic_workflow import dynamic_workflow
from lies.capabilities.file_system import file_system
from lies.capabilities.memory import memory
from lies.capabilities.planning import planning
from lies.capabilities.shell import shell


def test_code_mode_returns_capability() -> None:
    cap = code_mode()
    assert cap is not None


def test_memory_returns_capability() -> None:
    cap = memory()
    assert cap is not None


def test_planning_returns_capability() -> None:
    cap = planning()
    assert cap is not None


def test_dynamic_workflow_returns_capability() -> None:
    stub = Agent("test", name="stub")
    cap = dynamic_workflow(agents=[stub])
    assert cap is not None


def test_file_system_returns_capability(tmp_path: Path) -> None:
    cap = file_system(wiki_root=tmp_path)
    assert cap is not None


def test_shell_returns_capability() -> None:
    """Legacy ``shell(allowlist=...)`` is intentionally disabled.

    LIES exposes explicit Python wrappers (``constrained_tools``) instead
    of giving the model a shell allowlist. The compatibility shim raises
    so accidental use fails closed.
    """
    import pytest

    with pytest.raises(RuntimeError, match="arbitrary shell access is disabled"):
        shell(allowlist=["qmd", "git"])
