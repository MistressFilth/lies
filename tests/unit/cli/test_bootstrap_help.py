"""Pin help-text honesty for the bootstrap-on-missing commands.

Marked slow: the test invokes ``runner.invoke(app, ["sync", "--help"])``
which pays the full lies.cli import cost (~300ms). The assertion
itself is fast; the import is the dominant cost.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from lies.cli import app
from tests.unit.cli._ansi import strip_ansi

pytestmark = pytest.mark.slow

runner = CliRunner()


def test_sync_help_advertises_bootstrap() -> None:
    result = runner.invoke(app, ["sync", "--help"])
    assert result.exit_code == 0
    text = strip_ansi(result.output)
    assert "--source" in text
    assert "bootstrap" in text.lower()
