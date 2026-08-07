"""Shared fixtures for integration tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def child_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Snapshot the XDG-redirected env so subprocesses see the same wiki.

    The autouse ``_isolated_xdg`` fixture in ``tests/conftest.py`` already
    redirected the five XDG base directories into ``tmp_path``; this
    fixture captures those redirections in a form we can hand to
    ``subprocess.run(env=...)`` so a child Python process resolves the
    same wiki as the parent.
    """
    keys = [k for k in os.environ if k.startswith(("XDG_", "LIES_XDG_"))]
    return {k: os.environ[k] for k in keys}
