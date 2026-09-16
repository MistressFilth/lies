"""Unit tests for the MCP instructions/prompts loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from lies import __version__
from lies.mcp.instructions_loader import (
    INSTRUCTIONS_PATH,
    PROMPTS_DIR,
    load_instructions,
    load_prompt,
)


def test_instructions_path_resolves_under_mcp_package() -> None:
    assert INSTRUCTIONS_PATH.name == "instructions.md"
    assert INSTRUCTIONS_PATH.parent.name == "mcp"


def test_prompts_dir_resolves_under_mcp_package() -> None:
    assert PROMPTS_DIR.name == "prompts"
    assert PROMPTS_DIR.parent.name == "mcp"


def test_load_instructions_stamps_version() -> None:
    body = load_instructions()
    assert "$version" not in body, "unrendered template placeholder leaked"
    assert "LIES" in body or "lies" in body


def test_load_instructions_renders_literal_braces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`{...}` tokens in instructions.md render as literals, not format placeholders."""
    fake = tmp_path / "instructions.md"
    fake.write_text("path {slug} with $version\n", encoding="utf-8")
    monkeypatch.setattr("lies.mcp.instructions_loader.INSTRUCTIONS_PATH", fake)
    body = load_instructions()
    assert "{slug}" in body, "literal {slug} should survive Template substitution"
    assert __version__ in body


def test_load_prompt_stamps_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "fake.md"
    fake.write_text("hello $version\n", encoding="utf-8")
    monkeypatch.setattr("lies.mcp.instructions_loader.PROMPTS_DIR", tmp_path)
    body = load_prompt("fake")
    assert body == f"hello {__version__}\n"


def test_load_prompt_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lies.mcp.instructions_loader.PROMPTS_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        load_prompt("does-not-exist")
