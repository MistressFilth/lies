"""Top-level --name is removed; subcommand --name is preserved.

The top-level ``--name`` option on the Typer ``@app.callback`` was
registered (so Click/Typer accepted it) but never threaded into
subcommands that take their own ``--name`` (e.g. ``config``,
``lint``, ``collections show``). The operator typed
``lies --name claude-code lint`` and silently got the wrong wiki.

The fix: remove the top-level option entirely. The subcommand
``--name`` is the only path. After ``consolidate-wikis`` lands,
``--name`` is replaced by ``--project`` at the daemon level;
the top-level/subcommand distinction dissolves.

This test pins both halves:
- top-level ``--name`` errors with "no such option" (post-fix).
- subcommand ``--name`` does NOT error with "no such option"
  (regression guard against accidental removal).
"""

from __future__ import annotations

from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()


def test_top_level_name_option_errors_with_no_such_option() -> None:
    """`lies --name <wiki> <subcommand>` must error: no such option."""
    result = runner.invoke(app, ["--name", "default", "config"])
    assert result.exit_code == 2, (
        f"expected Click's 'no such option' exit code 2, got {result.exit_code}; "
        f"output was: {result.output!r}"
    )
    assert "no such option" in result.output.lower(), (
        f"expected 'no such option' in output, got: {result.output!r}"
    )


def test_subcommand_name_option_still_recognized() -> None:
    """`lies <subcommand> --name <wiki>` must not error with 'no such option'."""
    result = runner.invoke(app, ["config", "--name", "default"])
    # The subcommand --name option is still there. The result may fail
    # for other reasons (wiki not registered in test env, providers
    # config missing, etc.) but it must NOT fail with 'no such option'.
    assert "no such option" not in result.output.lower(), (
        f"subcommand --name should still be recognized; got: {result.output!r}"
    )
