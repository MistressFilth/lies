"""Tests for lies reindex CLI flags restored in F38."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.qmd import _models, cli as qmd_cli


@pytest.fixture
def tty_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_resolve_wiki():
    """Patch ``lies.cli.resolve_wiki``; return the wiki MagicMock.

    The CLI's ``reindex`` calls ``resolve_wiki(name)`` to load the wiki.
    Tests don't have a real on-disk wiki; mock the seam so the body runs
    without touching the filesystem. ``wiki_dir`` and ``raw_dir`` are
    real Paths because some test bodies read them (the reconcile path).
    """
    wiki = MagicMock()
    wiki.wiki_dir = Path("/tmp/wiki/wiki")
    wiki.raw_dir = Path("/tmp/wiki/raw")
    with patch("lies.cli.resolve_wiki", return_value=wiki):
        yield wiki


@pytest.fixture
def mock_qmd_reindex():
    """Patch ``qmd_reindex``; return a MagicMock for assertions.

    Patches the source module's attribute. The CLI re-imports
    ``qmd_reindex`` inside the function body on each invocation, so the
    patched source is picked up.
    """
    with patch.object(
        qmd_cli,
        "qmd_reindex",
        return_value=_models.ReindexResult(indexed=True),
    ) as mock:
        yield mock


def test_reindex_no_flags_calls_qmd_reindex(
    tty_runner: CliRunner,
    mock_resolve_wiki: MagicMock,
    mock_qmd_reindex: MagicMock,
) -> None:
    """``lies reindex`` alone shells out to ``qmd_reindex`` with no flags."""
    result = tty_runner.invoke(app, ["reindex", "--name", "t"])
    assert result.exit_code == 0, result.output
    mock_qmd_reindex.assert_called_once()
    kwargs = mock_qmd_reindex.call_args.kwargs
    assert kwargs.get("embed") is False
    assert kwargs.get("cleanup") is False
    assert kwargs.get("all_") is False
    assert kwargs.get("force") is False


def test_reindex_force_skips_confirm(
    tty_runner: CliRunner,
    mock_resolve_wiki: MagicMock,
    mock_qmd_reindex: MagicMock,
) -> None:
    """``--force`` alone (non-destructive) doesn't prompt."""
    with (
        patch("lies.cli._stdout_isatty", return_value=True),
        patch("builtins.input") as mock_input,
    ):
        result = tty_runner.invoke(app, ["reindex", "--name", "t", "--force"])
    mock_input.assert_not_called()
    assert result.exit_code == 0, result.output
    mock_qmd_reindex.assert_called_once()


def test_reindex_embed_skips_confirm(
    tty_runner: CliRunner,
    mock_resolve_wiki: MagicMock,
    mock_qmd_reindex: MagicMock,
) -> None:
    """``--embed`` alone (non-destructive) doesn't prompt."""
    with (
        patch("lies.cli._stdout_isatty", return_value=True),
        patch("builtins.input") as mock_input,
    ):
        result = tty_runner.invoke(app, ["reindex", "--name", "t", "--embed"])
    mock_input.assert_not_called()
    assert result.exit_code == 0, result.output
    mock_qmd_reindex.assert_called_once()


def test_reindex_cleanup_tty_yes_proceeds(
    tty_runner: CliRunner,
    mock_resolve_wiki: MagicMock,
    mock_qmd_reindex: MagicMock,
) -> None:
    """TTY + ``y`` on ``--cleanup``: qmd called."""
    with (
        patch("lies.cli._stdout_isatty", return_value=True),
        patch("builtins.input", return_value="y"),
    ):
        result = tty_runner.invoke(app, ["reindex", "--name", "t", "--cleanup"])
    assert result.exit_code == 0, result.output
    mock_qmd_reindex.assert_called_once()
    kwargs = mock_qmd_reindex.call_args.kwargs
    assert kwargs.get("cleanup") is True


def test_reindex_cleanup_tty_empty_exits(
    tty_runner: CliRunner,
    mock_resolve_wiki: MagicMock,
    mock_qmd_reindex: MagicMock,
) -> None:
    """TTY + empty on ``--cleanup``: exit 2, qmd not called."""
    with (
        patch("lies.cli._stdout_isatty", return_value=True),
        patch("builtins.input", return_value=""),
    ):
        result = tty_runner.invoke(app, ["reindex", "--name", "t", "--cleanup"])
    assert result.exit_code == 2
    mock_qmd_reindex.assert_not_called()


def test_reindex_cleanup_no_tty_no_yes_exits(
    tty_runner: CliRunner,
    mock_resolve_wiki: MagicMock,
    mock_qmd_reindex: MagicMock,
) -> None:
    """Non-TTY + no ``--yes`` on ``--cleanup``: exit 2, no prompt."""
    with (
        patch("lies.cli._stdout_isatty", return_value=False),
        patch("builtins.input") as mock_input,
    ):
        result = tty_runner.invoke(app, ["reindex", "--name", "t", "--cleanup"])
    mock_input.assert_not_called()
    assert result.exit_code == 2
    mock_qmd_reindex.assert_not_called()


def test_reindex_cleanup_no_tty_with_yes_proceeds(
    tty_runner: CliRunner,
    mock_resolve_wiki: MagicMock,
    mock_qmd_reindex: MagicMock,
) -> None:
    """Non-TTY + ``--yes`` on ``--cleanup``: qmd called, no prompt."""
    with (
        patch("lies.cli._stdout_isatty", return_value=False),
        patch("builtins.input") as mock_input,
    ):
        result = tty_runner.invoke(app, ["reindex", "--name", "t", "--cleanup", "--yes"])
    mock_input.assert_not_called()
    assert result.exit_code == 0, result.output
    mock_qmd_reindex.assert_called_once()


def test_reindex_all_tty_yes_proceeds(
    tty_runner: CliRunner,
    mock_resolve_wiki: MagicMock,
    mock_qmd_reindex: MagicMock,
) -> None:
    """``--all``: gated same as ``--cleanup``."""
    with (
        patch("lies.cli._stdout_isatty", return_value=True),
        patch("builtins.input", return_value="y"),
    ):
        result = tty_runner.invoke(app, ["reindex", "--name", "t", "--all"])
    assert result.exit_code == 0, result.output
    kwargs = mock_qmd_reindex.call_args.kwargs
    assert kwargs.get("all_") is True
