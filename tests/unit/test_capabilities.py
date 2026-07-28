from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent

from lies.capabilities.code_mode import code_mode
from lies.capabilities.dynamic_workflow import dynamic_workflow
from lies.capabilities.file_system import file_system
from lies.capabilities.memory import memory
from lies.capabilities.planning import planning


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
