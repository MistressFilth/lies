"""End-to-end integration test for ``lies page write`` (F39 + F12)."""

from __future__ import annotations

import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("INTEGRATION"),
    reason="set INTEGRATION=1 to run",
)


def test_page_write_then_overwrite(tmp_path):
    """First write -> create; second write without --force -> fail; with --force -> update."""
    env = os.environ.copy()
    env["LIES_WIKI_NAME"] = "test-integration"
    # The page-write path constructs the Orchestrator (which builds
    # every sub-agent's provider), so the subprocess needs *some*
    # ANTHROPIC_API_KEY to instantiate the AnthropicProvider even
    # though no agent call is ever made. A dummy value is enough;
    # pydantic-ai will only complain if a real call hits the wire.
    env.setdefault("ANTHROPIC_API_KEY", "test-integration-dummy-key-not-used")
    env.setdefault("MINIMAX_API_KEY", "test-integration-dummy-key-not-used")
    # The integration conftest's autouse ``_seed_librarian_model`` sets
    # ``LIES_LIBRARIAN_MODEL=test`` in the parent process; that env
    # override propagates via ``env.copy()`` and would beat the TOML
    # written below (which is the source of truth this test asserts
    # against). The "test" value is also malformed (no ``:``) so
    # ``parse_model_string`` would raise inside the subprocess's
    # orchestrator constructor. Drop it so the subprocess reads the
    # TOML the test just seeded.
    env.pop("LIES_LIBRARIAN_MODEL", None)

    # Init wiki + collection
    subprocess.run(["uv", "run", "lies", "init", "test-integration"], check=True, env=env)

    # Seed a providers.toml at the wiki's user-level providers path so
    # the Orchestrator can resolve every agent. The AnthropicProvider
    # only needs the env var at instantiation time; no live API call.
    from lies.wiki.wiki import Wiki
    from textwrap import dedent

    from lies.providers.agents import AGENT_ROSTER

    providers_path = Wiki.require("test-integration").providers_path
    providers_body = dedent(
        """
        default_model = "anthropic:claude-opus-4-7"

        [providers.anthropic]
        type = "anthropic"
        api_key_env = "ANTHROPIC_API_KEY"

        [providers.minimax]
        type = "anthropic_compatible"
        base_url = "https://api.minimax.io/anthropic"
        api_key_env = "MINIMAX_API_KEY"

        [agents]
        """
    ).lstrip()
    minimax_agents = {"source_reader", "page_writer"}
    agents_block = "\n".join(
        f'{n} = "minimax:MiniMax-M3"'
        if n in minimax_agents
        else f'{n} = "anthropic:claude-opus-4-7"'
        for n in AGENT_ROSTER
    )
    providers_path.write_text(providers_body + "\n" + agents_block + "\n")

    # Body must satisfy the schema's required sections for ``concept``
    # (## Definition, ## Examples, ## Related — see
    # src/lies/schema/default_schema.md).
    body = (
        "## Definition\nFirst version.\n\n"
        "## Examples\n- example 1\n- example 2\n\n"
        "## Related\n- sibling 1\n- sibling 2\n"
    )

    # First write
    result = subprocess.run(
        [
            "uv",
            "run",
            "lies",
            "page",
            "write",
            "--collection",
            "claude-code",
            "--type",
            "concept",
            "--slug",
            "hooks",
            "--title",
            "Hooks",
            "--body-file",
            "-",
            "--name",
            "test-integration",
        ],
        input=body,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"first write failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "create:" in result.stdout

    # Second write without --force
    result = subprocess.run(
        [
            "uv",
            "run",
            "lies",
            "page",
            "write",
            "--collection",
            "claude-code",
            "--type",
            "concept",
            "--slug",
            "hooks",
            "--title",
            "Hooks",
            "--body-file",
            "-",
            "--name",
            "test-integration",
        ],
        input=body,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2, (
        f"second write should exit 2 on collision: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # Third write with --force
    result = subprocess.run(
        [
            "uv",
            "run",
            "lies",
            "page",
            "write",
            "--collection",
            "claude-code",
            "--type",
            "concept",
            "--slug",
            "hooks",
            "--title",
            "Hooks",
            "--body-file",
            "-",
            "--force",
            "--name",
            "test-integration",
        ],
        input=body,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"third write (--force) failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "update:" in result.stdout
