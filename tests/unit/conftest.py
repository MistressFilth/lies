"""Unit-test-only fixtures and options.

Adds ``--runslow``: when absent, every test marked ``@pytest.mark.slow``
is skipped. The default ``make unit-test`` run skips them; CI's
``--runslow`` mode (or a developer chasing a regression) re-enables
them. The marker is registered in ``pyproject.toml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _stub_qmd_recycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the qmd daemon liveness probe for every test in ``tests/unit/``.

    Construction of ``QmdCapability`` calls ``_probe_liveness(url)``
    against the dev environment's running qmd daemon (e.g. on
    127.0.0.1:8181). Without this stub, a developer machine with a
    healthy daemon would still open a real TCP socket + JSON-RPC
    session from every orchestrator test — flake-prone across machines
    that have *no* daemon (probe raises, tests fall back, but the
    import path itself depends on daemon state). Tests in this
    directory don't exercise qmd probe/recycle behavior — that's
    ``test_qmd_capability.py``'s job. Stubbing the probe at the
    conftest level covers every orchestrator-using file
    (``test_orchestrator*``, ``test_orchestrator_lint``, etc.) and any
    future orchestrator test added here.

    Also stub ``qmd_daemon_reachable`` so the TCP-level check returns
    True, so the stubbed probe is actually exercised.
    """
    from lies.qmd.daemon import QmdState

    async def _stub_probe(url: str) -> None:
        return None

    async def _stub_recycle(*, data_dir: Path, daemon_url: str, **kwargs: object) -> QmdState:
        return QmdState(True, True, 1, "test stub")

    monkeypatch.setattr("lies.qmd.capability._probe_liveness", _stub_probe)
    monkeypatch.setattr(
        "lies.qmd.capability.qmd_daemon_reachable",
        lambda url, timeout=0.5: True,
    )


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
