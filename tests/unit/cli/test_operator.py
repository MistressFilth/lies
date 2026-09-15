"""Tests for the operator escape-hatch subcommands (``lies flock qmd recycle``).

The ``flock_app`` / ``_qmd_flock_app`` group has its own dedicated
test file (``test_flock_qmd.py``); this file covers the operator
escape-hatch subcommands that piggy-back on the same nested app.

See ``AGENTS.md`` for the project's monkeypatch-at-the-lies-module-boundary
testing convention.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from lies.cli.operator import _qmd_flock_app


def test_lies_flock_qmd_recycle_invokes_recycle_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`lies flock qmd recycle --name X` calls ``recycle_qmd_daemon`` and prints QmdState."""
    from lies.qmd.daemon import QmdState

    captured: dict[str, object] = {}

    async def _fake_recycle(*, data_dir, daemon_url, ready_timeout=30.0, **kwargs):
        captured["data_dir"] = data_dir
        captured["daemon_url"] = daemon_url
        captured["ready_timeout"] = ready_timeout
        return QmdState(True, True, 42, "manual recycle complete")

    monkeypatch.setattr("lies.qmd.daemon.recycle_qmd_daemon", _fake_recycle)

    fake_wiki = type("W", (), {"data_root": "/fake/data/root"})()
    monkeypatch.setattr(
        "lies.wiki.wiki.Wiki.require",
        classmethod(lambda cls, name: fake_wiki),
    )

    # Invoke the qmd sub-app directly so the PR #74 ``_FlockGroup`` bypass
    # logic doesn't need to be exercised here — the operator CLI is
    # tested via the ``flock_app`` group in ``test_flock_qmd.py`` instead.
    # ``get_command(_qmd_flock_app)`` returns a ``TyperGroup`` that lacks
    # ``_add_completion`` and trips ``CliRunner.invoke`` on this Typer
    # version; the existing ``test_flock_qmd.py`` tests use the same
    # pattern of passing the Typer app directly.
    runner = CliRunner()
    result = runner.invoke(_qmd_flock_app, ["recycle", "--name", "default"])
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    assert "manual recycle complete" in (result.stdout or "")
    assert captured["data_dir"] == "/fake/data/root"
    assert captured["ready_timeout"] == 30.0
