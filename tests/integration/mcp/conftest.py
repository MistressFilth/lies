"""Every test in this directory talks to a live qmd daemon.

The assertions are on what the daemon returns for the real corpus — a
body with no line-number prefixes, a collection filter honoured inside
qmd rather than afterwards. A stub cannot make those claims, so these
tests have no meaning without a daemon, and a CI runner has none.

`test_tag_filter_end_to_end.py` gates on the same condition at fixture
setup; this directory gates once, here, so a file added under it later
inherits the guard instead of failing CI with `QmdDaemonUnavailable`.

The trade is stated rather than hidden: **these tests provide no CI
coverage.** They run where a daemon is up. A runner that wants them
needs a qmd daemon service, which the workflow does not start.
"""

from __future__ import annotations

import pytest

from lies.config import DEFAULT_QMD_URL
from lies.qmd import health

# Reached as ``health.qmd_daemon_reachable`` rather than imported by name,
# so a test that patches the module attribute patches what this guard
# actually calls. Importing the function directly binds it here, and a
# monkeypatch on the module then leaves this call site pointing at the
# original — which makes the guard impossible to exercise without a
# real daemon.


@pytest.fixture(autouse=True)
def _require_live_qmd_daemon() -> None:
    """Skip when no qmd daemon is serving at ``DEFAULT_QMD_URL``."""
    if not health.qmd_daemon_reachable(DEFAULT_QMD_URL, timeout=0.5):
        pytest.skip(
            f"qmd daemon is not serving at {DEFAULT_QMD_URL}; "
            f"start it with 'lies qmd up'. These tests assert on what the "
            f"live daemon returns, so a stub would not make the claim."
        )
