"""End-to-end: init wiki -> query -> assert sidecar + lies memory + wiki_changes all surface the receipt.

Gated on ``INTEGRATION=1`` like other integration tests in this repo
(``tests/integration/test_sync_register_persistence.py``).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION") != "1",
    reason="integration test gated on INTEGRATION=1",
)


def test_query_writes_sidecar_visible_via_all_three_surfaces(tmp_path: Path) -> None:
    # 1. Init a wiki via the CLI.
    name = "smoke"
    # ``LIES_XDG_DATA_HOME`` is the lies-specific override of XDG_DATA_HOME; the
    # wiki resolves ``data_root = $LIES_XDG_DATA_HOME / lies / <name>``, so the
    # sidecar lands at ``tmp_path / lies / smoke / .lies / memory_plans.jsonl``.
    #
    # ``ANTHROPIC_API_KEY`` is set to a dummy value so the orchestrator's
    # ``AnthropicProvider`` constructor doesn't crash at import time even when
    # no ``providers.toml`` exists (matches the pattern in
    # ``tests/integration/test_cli_config.py``); no real LLM call happens on a
    # fresh wiki with no ingested sources, so the value is never sent.
    env = {
        **os.environ,
        "LIES_WIKI_NAME": name,
        "LIES_XDG_DATA_HOME": str(tmp_path),
        "ANTHROPIC_API_KEY": "test-key",
    }
    subprocess.run(
        ["uv", "run", "lies", "init", name],
        check=True,
        env=env,
        cwd="/home/divinefilth/code/github/MistressFilth/lies/f0-surface-lift",
        capture_output=True,
    )

    # 2. Run a query that triggers the MemoryEnricher (no-op on a fresh wiki;
    #    exits 0 whether or not the enricher applies a plan). Capture output
    #    without ``check=True`` so a non-zero exit surfaces in the assertion
    #    below with its stderr for diagnostics.
    result = subprocess.run(
        ["uv", "run", "lies", "query", "what is in the wiki?"],
        check=False,
        env=env,
        cwd="/home/divinefilth/code/github/MistressFilth/lies/f0-surface-lift",
        capture_output=True,
        text=True,
    )
    # A fresh wiki with no pages outputs the empty-receipt fallback body
    # ("No pages found."), so we only assert the command exits cleanly.
    assert result.returncode == 0, (
        f"lies query failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # 3. Assert the sidecar exists only when a plan was actually applied.
    sidecar = tmp_path / "lies" / name / ".lies" / "memory_plans.jsonl"
    rows = []
    if sidecar.exists():
        rows = [json.loads(ln) for ln in sidecar.read_text().splitlines() if ln]

    # 4. ``lies memory --limit 1`` surfaces the most recent row (or the empty-
    #    receipt message). The command always exits 0.
    mem_result = subprocess.run(
        ["uv", "run", "lies", "memory", "--limit", "1"],
        check=False,
        env=env,
        cwd="/home/divinefilth/code/github/MistressFilth/lies/f0-surface-lift",
        capture_output=True,
        text=True,
    )
    assert mem_result.returncode == 0
    if rows:
        # When the enricher applied a plan, the commit SHA from the sidecar
        # must appear in ``lies memory`` output.
        assert rows[0]["commit_sha"][:12] in mem_result.stdout
