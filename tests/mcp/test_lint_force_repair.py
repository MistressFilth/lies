"""FastMCP `lint(fix=True, force_repair=True)` propagation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lies.lock_errors import WikiFlockUnrepairable
from lies.mcp.server import lint
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
    wiki: None,
) -> None:
    err = WikiFlockUnrepairable(
        "memory flock for wiki 'mywiki' held by live pid 12345 (started T); "
        "force-repair failed after retry. Run `lies flock mywiki force-repair`."
    )
    with patch("lies.mcp.server.Orchestrator") as mock_orch:
        mock_orch.return_value.run_lint.side_effect = err
        result = lint("mywiki", fix=True, force_repair=True)
    assert "pid 12345" in result
    assert "lies flock mywiki force-repair" in result
