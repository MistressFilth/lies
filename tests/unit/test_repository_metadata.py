from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_ci_uses_required_check_context_and_make_targets() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"check"}
    steps = workflow["jobs"]["check"]["steps"]
    actions = {step.get("uses", "") for step in steps}
    assert "actions/checkout@v7" in actions
    assert "actions/setup-python@v7" in actions
    assert "astral-sh/setup-uv@v9" in actions
    commands = [step.get("run", "") for step in steps]
    assert "uv sync --all-extras" in commands
    assert "make check" in commands
    assert "git diff --exit-code" in commands
    assert "make test" in commands


def test_mypy_is_absent_from_repository_configuration() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    precommit = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "[tool.mypy]" not in pyproject
    for label, text in [
        (".github/workflows/ci.yml", workflow),
        ("README.md", readme),
        ("Makefile", makefile),
        (".pre-commit-config.yaml", precommit),
    ]:
        assert re.search(r"\bmypy\b", text, re.IGNORECASE) is None, f"{label} still references mypy"


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
