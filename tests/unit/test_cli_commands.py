from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()


def test_init_creates_wiki(tmp_path: Path) -> None:
    with patch("lies.cli.Orchestrator"):
        result = runner.invoke(app, ["init", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / "raw").exists()
        assert (tmp_path / "wiki").exists()
        assert (tmp_path / ".lies").exists()
        # .lies/schema.md copied from default
        assert (tmp_path / ".lies" / "schema.md").exists()


def test_ingest_invokes_orchestrator(tmp_path: Path) -> None:
    with patch("lies.cli.Orchestrator") as MockOrch:
        mock_instance = MockOrch.return_value
        mock_instance.run_ingest.return_value = "ingested ok"
        result = runner.invoke(app, ["ingest", "raw/article.md", "--wiki-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "ingested ok" in result.stdout
        mock_instance.run_ingest.assert_called_once()
        call_arg = mock_instance.run_ingest.call_args.args[0]
        assert "raw/article.md" in call_arg


def test_query_invokes_orchestrator(tmp_path: Path) -> None:
    with patch("lies.cli.Orchestrator") as MockOrch:
        mock_instance = MockOrch.return_value
        mock_instance.run_query.return_value = _StubAnswer("the answer")
        result = runner.invoke(app, ["query", "What is X?", "--wiki-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "the answer" in result.stdout


def test_lint_invokes_orchestrator(tmp_path: Path) -> None:
    with patch("lies.cli.Orchestrator") as MockOrch:
        mock_instance = MockOrch.return_value
        mock_instance.run_lint.return_value = "3 findings"
        result = runner.invoke(app, ["lint", "--wiki-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "3 findings" in result.stdout


class _StubAnswer:
    """Minimal stand-in for ``SynthesizedAnswer`` in CLI tests.

    The CLI only reads ``.answer`` from the result, so a tiny stub with
    that attribute is enough to keep the markdown printer happy without
    importing the real pydantic dataclass.
    """

    def __init__(self, answer: str) -> None:
        self.answer = answer


def test_status_invokes_qmd(tmp_path: Path) -> None:
    with patch("lies.cli.qmd_status") as mock_status:
        mock_status.return_value = "indexed: 42 pages"
        result = runner.invoke(app, ["status", "--wiki-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "indexed: 42 pages" in result.stdout
