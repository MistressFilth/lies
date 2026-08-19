"""FastMCP `lint(fix=True, force_repair=True)` propagation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lies.lock_errors import WikiFlockUnrepairable
from lies.mcp.server import lint
from lies.memory.service import _acquire_wiki_flock
from lies.wiki.wiki import Wiki


@pytest.fixture
def wiki() -> None:
    """Register a minimal ``mywiki`` so :func:`resolve_wiki` succeeds.

    ``Wiki.require`` only verifies the data-root directory exists, so
    a real git working tree is unnecessary — the test mocks
    ``Orchestrator`` entirely and never invokes the underlying pipeline.
    """
    data_root = Wiki.data_root_for("mywiki")
    data_root.mkdir(parents=True, exist_ok=True)


def test_lint_tool_with_force_repair_propagates_unrepairable_as_string(
    wiki: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real-path test for the MCP tool's ``force_repair=True`` propagation.

    Drives the production-raised ``WikiFlockUnrepairable`` message
    constructed by ``_acquire_wiki_flock`` (Task 2) through the MCP
    tool surface. The tool catches the flock error and returns it as
    an ``error:``-prefixed string.

    Per the M1 spec's "Risks + mitigations" section, the
    ``WikiFlockUnrepairable`` from ``_acquire_wiki_flock`` deliberately
    omits pid (the file is unlinked before the retry); the
    spec-mandated substring is ``lies flock mywiki force-repair``.
    """
    # Derive the expected message from the production raise site rather
    # than hand-copying it: a reword in ``service.py:100-106`` would
    # otherwise leave this test green while asserting stale text. The
    # stub forces ``acquire_create_lock`` to return ``None`` so the
    # production ``if result is None and force_repair`` branch fires.
    import lies.memory.service as service_module

    def _stub_acquire(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(service_module, "acquire_create_lock", _stub_acquire)

    fake_wiki = Wiki(
        name="mywiki",
        data_root=Path("/tmp/fake-mcp-wiki-mywiki"),
        config_root=Path("/tmp/fake-mcp-wiki-mywiki/config"),
        cache_root=Path("/tmp/fake-mcp-wiki-mywiki/cache"),
        state_root=Path("/tmp/fake-mcp-wiki-mywiki/state"),
        runtime_root=Path("/tmp/fake-mcp-wiki-mywiki/runtime"),
    )
    try:
        _acquire_wiki_flock(fake_wiki, force_repair=True).__enter__()
    except WikiFlockUnrepairable as exc:
        production_msg = str(exc)
        assert production_msg, "production raise site produced an empty message"
    else:
        pytest.fail(
            "_acquire_wiki_flock did not raise WikiFlockUnrepairable; "
            "test setup is broken (acquire_create_lock stub not engaged?)"
        )

    # Real Orchestrator instance via ``__new__`` (same seam as the
    # CLI lint test) instead of a ``patch(...)`` MagicMock — the
    # hand-crafted ``side_effect=err`` previously asserted a string the
    # test itself wrote, the same defect Task 4 was supposed to
    # eliminate.
    from lies.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.run_lint = MagicMock(side_effect=WikiFlockUnrepairable(production_msg))

    monkeypatch.setattr("lies.mcp.server.Orchestrator", lambda *_a, **_kw: orch)

    result = lint("mywiki", fix=True, force_repair=True)
    assert f"error: {production_msg}" == result
