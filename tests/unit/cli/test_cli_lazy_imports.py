"""Verify that the CLI package does not import heavy modules at startup.

The point of the lazy-cli-imports refactor is that `lies --help` and
other model-free commands should not pay the cost of loading the
orchestrator, the pydantic-ai stack, or the anthropic SDK. This file
pins that property as a regression test.

We use subprocess invocations for the assertions because in-process
sys.modules manipulation pollutes the pytest session and breaks
unrelated tests (notably the REPL tests in tests/unit/test_cli.py).
A clean Python process is the only reliable way to measure what
`import lies.cli` does at startup.
"""

from __future__ import annotations

import subprocess


def _run_in_clean_process(snippet: str) -> str:
    """Run `python -c <snippet>` in a fresh interpreter and return stdout."""
    result = subprocess.run(
        ["uv", "run", "python", "-c", snippet],
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
