"""Shared pytest fixtures."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure each test starts with a clean LIES_* env."""
    for key in list(os.environ):
        if key.startswith("LIES_"):
            monkeypatch.delenv(key, raising=False)