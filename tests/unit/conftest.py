"""Unit-test-only fixtures and options.

Adds ``--runslow``: when absent, every test marked ``@pytest.mark.slow``
is skipped. The default ``make unit-test`` run skips them; CI's
``--runslow`` mode (or a developer chasing a regression) re-enables
them. The marker is registered in ``pyproject.toml``.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runslow",
        action="store_true",
        default=False,
        help="run tests marked as slow (default: skip them)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip slow-marked items unless ``--runslow`` was passed."""
    if config.getoption("--runslow"):
        return
    skip_slow = pytest.mark.skip(reason="slow test; run with --runslow to enable")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
