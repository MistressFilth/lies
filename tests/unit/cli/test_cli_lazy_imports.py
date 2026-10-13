"""Verify that the CLI package does not import heavy modules at startup."""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.slow


def _run_in_clean_process(snippet: str) -> str:
    """Run `python -c <snippet>` in a fresh interpreter and return stdout.

    Bypasses ``uv run`` to avoid its ~150ms of project lookup overhead;
    the test runs under pytest's own venv, so ``sys.executable`` already
    points at the right interpreter with the right site-packages.
    """
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_lies_cli_does_not_load_orchestrator() -> None:
    """`import lies.cli` in a fresh process must not load the orchestrator."""
    out = _run_in_clean_process(
        "import sys\nimport lies.cli\nprint('lies.orchestrator' in sys.modules)\n"
    )
    assert out == "False", (
        f"lies.cli pulled in lies.orchestrator. Expected 'False', got {out!r}. "
        "Move the orchestrator import into the command bodies that need it."
    )


def test_lies_cli_does_not_load_anthropic_sdk() -> None:
    """`import lies.cli` in a fresh process must not load the anthropic SDK."""
    out = _run_in_clean_process("import sys\nimport lies.cli\nprint('anthropic' in sys.modules)\n")
    assert out == "False", (
        f"lies.cli pulled in the anthropic SDK. Expected 'False', got {out!r}. "
        "The AnthropicModel import boundary is owned by pydantic_ai; do not "
        "eagerly import lies.providers at CLI startup."
    )


def test_lies_cli_does_not_load_pydantic_ai() -> None:
    """`import lies.cli` in a fresh process must not load pydantic_ai."""
    out = _run_in_clean_process(
        "import sys\nimport lies.cli\nprint('pydantic_ai' in sys.modules)\n"
    )
    assert out == "False", (
        f"lies.cli pulled in pydantic_ai. Expected 'False', got {out!r}. "
        "Move heavy transitive imports out of cli/_helpers.py and into "
        "command bodies. Profile with `python -X importtime -c 'import lies.cli'`."
    )


def test_lies_cli_does_not_load_fastmcp() -> None:
    """`import lies.cli` in a fresh process must not load fastmcp."""
    out = _run_in_clean_process("import sys\nimport lies.cli\nprint('fastmcp' in sys.modules)\n")
    assert out == "False", (
        f"lies.cli pulled in fastmcp. Expected 'False', got {out!r}. "
        "The MCP server SDK is only needed by `lies mcp up`; do not import "
        "it from cli/operator.py at module top."
    )
