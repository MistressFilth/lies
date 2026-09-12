"""Shared fixtures + integration gate for tests/integration/.

Every test in this directory skips unless ``INTEGRATION=1`` is set in the
environment. This is the single gate; individual files no longer need to
sprinkle their own ``pytestmark = pytest.mark.skipif(...)`` decorators.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

INTEGRATION_ENABLED = os.environ.get("INTEGRATION") == "1"
INTEGRATION_ROOT = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every item collected under ``tests/integration/`` unless opted in.

    The hook fires for the whole session, so path-filter before tagging —
    without this guard the marker would land on unit tests too.
    """
    if INTEGRATION_ENABLED:
        return
    skip = pytest.mark.skip(reason="integration tests gated on INTEGRATION=1")
    for item in items:
        if item.path.is_relative_to(INTEGRATION_ROOT):
            item.add_marker(skip)


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
