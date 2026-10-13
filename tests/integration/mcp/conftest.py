"""Every test here needs a live qmd daemon serving the corpus; see AGENTS.md."""

from __future__ import annotations

import pytest

from lies.config import DEFAULT_QMD_URL
from lies.qmd import health

# Reached as ``health.qmd_daemon_reachable`` rather than imported by name,
# so a monkeypatch of the module attribute reaches what this guard calls.


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
