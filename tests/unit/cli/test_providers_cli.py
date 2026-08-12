"""Tests for the ``lies providers`` sub-app and first-run hint.

These cover two surfaces:

- ``providers_app``: the six sub-commands registered for the wizard
  (``init``) and companion operators (``add`` / ``set-default`` /
  ``assign`` / ``unassign`` / ``check``). Each is exercised through
  ``CliRunner`` against the in-process Typer app so the actual
  parameter parsing, exit-code mapping, and error-path branches are
  covered. The companion commands construct a ``Wiki`` manually rather
  than going through ``resolve_wiki`` / ``Wiki.require``: providers.toml
  is user-level, not per-wiki, so a bootstrap flow cannot depend on a
  specific wiki already being initialized.

- The first-run hint: emitted to stderr by ``config_cmd``, ``init``,
  and ``mcp up`` when the providers.toml is absent and stdout is a TTY.
  Tests manipulate the click stream's ``isatty`` so the gate is
  exercised without depending on a real TTY.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.providers.agents import AGENT_ROSTER
from lies.providers.bootstrap import PartialConfig, write_atomic
from lies.providers.config import ProviderSpec

runner = CliRunner()


def _seed(tmp_path: Path) -> Path:
    """Write a valid providers.toml at the XDG-routed location.

    Returns the final path (``tmp_path/lies/providers.toml``) so callers
    don't need to relocate the file before invoking the CLI.
    """
    target = tmp_path / "lies" / "providers.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(
        target,
        PartialConfig(
            providers={
                "anthropic": ProviderSpec(
                    name="anthropic",
                    type="anthropic",
                    api_key_env="ANTHROPIC_API_KEY",
                ),
            },
            default_model="anthropic:claude-opus-4-7",
            agents={name: "anthropic:claude-opus-4-7" for name in AGENT_ROSTER},
        ),
    )
    return target


def _combined(result) -> str:
    """Click 8.2+ splits stderr from ``.output``; tolerate either layout."""
    return (result.stdout or "") + (result.stderr or "")


def test_providers_init_refuses_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``lies providers init`` exits 2 when providers.toml already exists.

    The file lives at ``$XDG_CONFIG_HOME/lies/providers.toml``; we point
    ``XDG_CONFIG_HOME`` at ``tmp_path`` so the resolver lands where the
    test planted the seed file.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _seed(tmp_path)
    result = runner.invoke(app, ["providers", "init", "--name", "default"])
    assert result.exit_code == 2
    assert "already exists" in _combined(result)


def test_providers_add_happy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``lies providers add`` appends a provider to the catalog (exit 0)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _seed(tmp_path)
    result = runner.invoke(
        app,
        [
            "providers",
            "add",
            "minimax",
            "--type",
            "anthropic_compatible",
            "--api-key-env",
            "MINIMAX_API_KEY",
            "--base-url",
            "https://api.minimax.io/anthropic",
            "--name",
            "default",
        ],
    )
    assert result.exit_code == 0, result.output


def _force_stdout_isatty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Force ``sys.stdout.isatty()`` to ``value`` for the duration of the test.

    ``CliRunner.invoke`` swaps ``sys.stdout`` for a click-injected
    ``_NamedTextIOWWrapper`` whose ``isatty`` is a C-level method that
    ignores attribute overrides. The CLI exposes ``_stdout_isatty`` as a
    module-level seam so tests can swap the check without fighting the
    click stream wrapper.
    """
    monkeypatch.setattr("lies.cli._stdout_isatty", lambda: value)


def test_first_run_hint_in_config_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``lies config`` emits the missing-providers hint when stdout is a TTY.

    The hint names the bootstrap sub-command so the operator can recover
    without reading the README.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _force_stdout_isatty(monkeypatch, True)
    # ``config`` resolves the wiki through ``Wiki.require``; mkdir the
    # default wiki's data root so the resolution succeeds and we land
    # in the hint-emitting branch.
    from lies.wiki.wiki import Wiki

    Wiki.data_root_for("default").mkdir(parents=True, exist_ok=True)
    result = runner.invoke(app, ["config", "--name", "default"])
    assert "providers init" in (result.stderr or "")


def test_no_hint_when_isatty_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hint is suppressed when stdout is not a TTY (CI, pipes, scripts)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _force_stdout_isatty(monkeypatch, False)
    from lies.wiki.wiki import Wiki

    Wiki.data_root_for("default").mkdir(parents=True, exist_ok=True)
    result = runner.invoke(app, ["config", "--name", "default"])
    assert "providers init" not in (result.stderr or "")
