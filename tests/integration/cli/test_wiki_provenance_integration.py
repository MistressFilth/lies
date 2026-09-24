"""Integration test for F29 ``lies wiki provenance`` end-to-end.

Confirms the helper + CLI + catalog layer agree when a synthesis page
is written via the real ``Orchestrator.file_back_author`` path (not a
fixture-driven upsert). Marker: ``integration`` (Makefile gates on
``INTEGRATION=1``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli.wiki import wiki_app


@pytest.mark.integration
def test_provenance_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write a synthesis page through the real orchestrator, then read it via the CLI."""
    from lies.orchestrator import Orchestrator
    from lies.page import build_author_plan
    from lies.wiki.wiki import Wiki

    data_root = tmp_path / "test-wiki"
    wiki_root = data_root / "wiki"
    wiki_root.mkdir(parents=True)
    wiki = Wiki(
        name="test-wiki",
        data_root=data_root,
        config_root=tmp_path / "config",
        cache_root=tmp_path / "cache",
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )

    # file_back_author snapshots via ``git``; init a working repo + config so
    # the atomic-commit envelope succeeds. (Matches the pattern in
    # ``tests/integration/test_lint_repair.py``.)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(data_root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(data_root), "config", "user.email", "test@test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(data_root), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )

    # Pre-create a source page the synthesis will cite.
    src_dir = wiki_root / "default" / "concepts"
    src_dir.mkdir(parents=True)
    (src_dir / "x.md").write_text("---\ntitle: X\ntype: concept\n---\n", encoding="utf-8")

    # Initial commit so ``file_back_author``'s working-tree snapshot has
    # something to diff against.
    subprocess.run(
        ["git", "-C", str(data_root), "add", "-A"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(data_root), "commit", "-m", "seed"],
        check=True,
        capture_output=True,
    )

    monkeypatch.setenv("LIES_WIKI_NAME", "test-wiki")
    # Orchestrator instantiation loads the providers stack which requires
    # ANTHROPIC_API_KEY even when no model call happens. The no-default-
    # model contract added in v0.38.0 also requires each agent slot to
    # have an explicit LIES_<AGENT>_MODEL override; set all of them to a
    # dummy value so the provider stack builds.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-integration-dummy-key-not-used")
    for agent_name in (
        "orchestrator",
        "source_reader",
        "page_writer",
        "linter",
        "query_synthesizer",
        "enricher",
        "repair",
    ):
        monkeypatch.setenv(f"LIES_{agent_name.upper()}_MODEL", "anthropic:test-dummy")
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda _name=None: wiki)

    orch = Orchestrator(wiki)
    plan = build_author_plan(
        type="synthesis",
        collection="default",
        slug="first",
        title="First",
        body="# Body\n",
        derived_from=["default/concepts/x"],
        tags=[],
        sources=[],
        exists=lambda r: (wiki.wiki_dir / r).exists(),
        sha_lookup=lambda r: orch._memory_service.current_state(r)[0],
    )
    receipt = orch.file_back_author(plan)
    assert not receipt.errors, receipt.errors

    runner = CliRunner()
    # Typer 0.27 flattens single-command Typer apps, so wiki_app is
    # invoked directly without a "provenance" prefix.
    result = runner.invoke(wiki_app, ["--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    slugs = {row["slug"] for row in payload}
    assert "default/synthesis/first" in slugs
    rec = next(r for r in payload if r["slug"] == "default/synthesis/first")
    assert rec["derived_from"] == ["default/concepts/x"]
