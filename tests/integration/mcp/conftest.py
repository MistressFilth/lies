"""Every test in this directory talks to a live qmd daemon.

The assertions are on what the daemon returns for the real corpus — a
body with no line-number prefixes, a collection filter honoured inside
qmd rather than afterwards. A stub cannot make those claims, so these
tests have no meaning without a daemon serving that corpus, and a CI
runner has none.

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
    """Skip unless a daemon is serving the corpus these tests assert on.

    Reachability alone is not the condition these tests need. A daemon on
    8181 that answers TCP but serves an *empty* index — a stray started by
    something else on the host — used to pass this guard and then fail
    every test beneath it (`ToolError: all reads failed`, and
    `assert frozenset()` from a daemon reporting no collections). The
    verdict must not depend on a host's stray process, so the guard asks
    the question the assertions actually depend on: is a corpus indexed
    there?
    """
    if not health.qmd_daemon_reachable(DEFAULT_QMD_URL, timeout=0.5):
        pytest.skip(
            f"qmd daemon is not serving at {DEFAULT_QMD_URL}; "
            f"start it with 'lies qmd up'. These tests assert on what the "
            f"live daemon returns, so a stub would not make the claim."
        )
    if not _daemon_serves_a_corpus():
        pytest.skip(
            f"a qmd daemon answers at {DEFAULT_QMD_URL} but serves no "
            f"collections. That is a daemon with an empty or foreign index, "
            f"not the corpus-serving one these tests assert against — a "
            f"stray local process bound to the port would otherwise decide "
            f"whether they run. Start the real daemon ('lies qmd up') or "
            f"point LIES_QMD_URL at it."
        )


def _daemon_serves_a_corpus() -> bool:
    """True when the daemon at ``DEFAULT_QMD_URL`` reports any collection.

    Reached through :func:`lies.qmd.access.validate_scope`'s own seam
    (``qmd_collection_names``) rather than a second, ad-hoc status call,
    so the guard and production agree on what "served" means.
    """
    import asyncio

    from lies.qmd import access

    try:
        return bool(asyncio.run(access.qmd_collection_names()))
    except Exception:  # noqa: BLE001 - a probe failure is not a corpus verdict
        return False
