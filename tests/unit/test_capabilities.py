from __future__ import annotations

from lies.capabilities.code_mode import code_mode
from lies.capabilities.memory import memory


def test_code_mode_returns_capability() -> None:
    cap = code_mode()
    assert cap is not None


def test_memory_returns_capability() -> None:
    cap = memory()
    assert cap is not None