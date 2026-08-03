from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_ci_uses_required_check_context_and_make_targets() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert set(workflow["jobs"]) == {"check"}
    commands = [step.get("run", "") for step in workflow["jobs"]["check"]["steps"]]
    assert "uv sync --all-extras" in commands
    assert "make check" in commands
    assert "git diff --exit-code" in commands
    assert "make test" in commands


def test_mypy_is_absent_from_repository_configuration() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "[tool.mypy]" not in pyproject
    assert re.search(r"\bmypy\b", workflow, re.IGNORECASE) is None
    assert re.search(r"\bmypy\b", readme, re.IGNORECASE) is None


def test_readme_has_badge_and_required_links() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "actions/workflows/ci.yml/badge.svg" in readme
    assert "CHANGELOG.md" in readme
    assert "AGENTS.md" in readme


def test_precommit_runs_full_test_gate_at_commit_stage() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    assert "test" in hooks
    assert hooks["test"]["entry"] == "make test"
    assert hooks["test"]["stages"] == ["pre-commit"]


def test_mit_license_exists() -> None:
    license_path = ROOT / "LICENSE"
    assert license_path.exists()
    license_text = license_path.read_text(encoding="utf-8")
    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 MistressFilth" in license_text


def test_pytest_registers_integration_marker() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        '"integration: end-to-end tests that exercise git and filesystem boundaries"' in pyproject
    )
