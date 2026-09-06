"""Test isolation for the providers package.

The keychain module keeps a process-global memo of resolved env-var
names. Without clearing it between tests, memo state leaks across
boundaries and produces order-dependent failures.
"""

from __future__ import annotations

import pytest

from lies.providers import keychain


@pytest.fixture(autouse=True)
def _clear_keychain_memo() -> None:
    keychain.invalidate()
    yield
    keychain.invalidate()
