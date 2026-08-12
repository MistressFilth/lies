"""End-to-end wizard run via subprocess + PTY. Gated on INTEGRATION=1.

Skipped by default so a developer running ``make test`` does not see a
hang. CI does NOT set INTEGRATION=1 for the default run; an opt-in
job in the workflow sets it and runs this file only.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from lies.providers import AGENT_ROSTER, load_providers_config

pytestmark = pytest.mark.skipif(
    not os.environ.get("INTEGRATION"),
    reason="integration test; set INTEGRATION=1 to enable",
)


def test_wizard_dry_run(tmp_path):
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(tmp_path)
    env["HOME"] = str(tmp_path)
    env["LIES_PROVIDERS_PRESET"] = ""  # placeholder

    answers = (
        "anthropic:claude-opus-4-7\n"  # default_model
        "yes\n"  # edit providers catalog?
        "minimax\n"  # provider name
        "anthropic_compatible\n"  # type
        "MINIMAX_API_KEY\n"  # api_key_env
        "https://api.minimax.io/anthropic\n"  # base_url
        "\n"  # blank -> stop providers
        "yes\n"  # assign to all agents
        "yes\n"  # confirm write
    )
    proc = subprocess.run(
        [sys.executable, "-m", "lies", "providers", "init", "--name", "default", "--force"],
        input=answers,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # assert on returncode below so stderr reaches the report
    )
    assert proc.returncode == 0, proc.stderr
    target = tmp_path / "lies" / "providers.toml"
    assert target.exists()

    # The file must be reloadable and carry everything the wizard was
    # driven to produce -- existence alone would pass on a truncated or
    # half-written file.
    loaded = load_providers_config(target)
    assert loaded is not None
    assert loaded.default_model == "anthropic:claude-opus-4-7"

    minimax = loaded.providers["minimax"]
    assert minimax.type == "anthropic_compatible"
    assert minimax.api_key_env == "MINIMAX_API_KEY"
    assert minimax.base_url == "https://api.minimax.io/anthropic"

    assert set(AGENT_ROSTER) <= set(loaded.agents), (
        f"missing agents: {sorted(set(AGENT_ROSTER) - set(loaded.agents))}"
    )
    assert all(loaded.agents[a] == "anthropic:claude-opus-4-7" for a in AGENT_ROSTER)
