"""Tests for the _confirm_destructive_cli helper."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import typer

import lies.cli as cli_module
from lies.cli._helpers import _confirm_destructive_cli


def test_assume_yes_skips_prompt() -> None:
    """assume_yes=True: no input() call, no exit."""
    with patch("builtins.input") as mock_input:
        _confirm_destructive_cli("msg", assume_yes=True)
    mock_input.assert_not_called()


def test_tty_yes_proceeds() -> None:
    """TTY + 'y' input: no exit."""
    with (
        patch.object(cli_module, "_stdout_isatty", return_value=True),
        patch("builtins.input", return_value="y"),
    ):
        _confirm_destructive_cli("msg")
    # No exception


def test_tty_yes_full_word_proceeds() -> None:
    """TTY + 'yes' input: no exit."""
    with (
        patch.object(cli_module, "_stdout_isatty", return_value=True),
        patch("builtins.input", return_value="yes"),
    ):
        _confirm_destructive_cli("msg")


def test_tty_empty_exits() -> None:
    """TTY + empty input: typer.Exit(2)."""
    with (
        patch.object(cli_module, "_stdout_isatty", return_value=True),
        patch("builtins.input", return_value=""),
    ):
        with pytest.raises(typer.Exit) as exc_info:
            _confirm_destructive_cli("msg")
        assert exc_info.value.exit_code == 2


def test_tty_n_exits() -> None:
    """TTY + 'n' input: typer.Exit(2)."""
    with (
        patch.object(cli_module, "_stdout_isatty", return_value=True),
        patch("builtins.input", return_value="n"),
    ):
        with pytest.raises(typer.Exit) as exc_info:
            _confirm_destructive_cli("msg")
        assert exc_info.value.exit_code == 2


def test_non_tty_no_yes_exits() -> None:
    """Non-TTY + no --yes: typer.Exit(2), no prompt."""
    with (
        patch.object(cli_module, "_stdout_isatty", return_value=False),
        patch("builtins.input") as mock_input,
    ):
        with pytest.raises(typer.Exit) as exc_info:
            _confirm_destructive_cli("msg", assume_yes=False)
        mock_input.assert_not_called()
        assert exc_info.value.exit_code == 2


def test_non_tty_with_yes_proceeds() -> None:
    """Non-TTY + --yes: no prompt, no exit."""
    with (
        patch.object(cli_module, "_stdout_isatty", return_value=False),
        patch("builtins.input") as mock_input,
    ):
        _confirm_destructive_cli("msg", assume_yes=True)
    mock_input.assert_not_called()
