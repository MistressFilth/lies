"""Unit tests: every prompt body loads + version-stamps."""

from __future__ import annotations

import pytest

from lies.mcp.instructions_loader import PROMPTS_DIR, load_prompt

EXPECTED_PROMPTS = {"orient", "ingest", "lint", "sync", "file-back"}


def test_prompts_dir_exists() -> None:
    assert PROMPTS_DIR.is_dir(), f"missing {PROMPTS_DIR}"


def test_all_expected_prompt_files_present() -> None:
    on_disk = {p.stem for p in PROMPTS_DIR.glob("*.md")}
    missing = EXPECTED_PROMPTS - on_disk
    assert not missing, f"missing prompt files: {sorted(missing)}"


def test_orient_prompt_loads_and_stamps_version() -> None:
    body = load_prompt("orient")
    assert "{version}" not in body
    assert "LIES" in body or "lies" in body


@pytest.mark.parametrize("name", sorted(EXPECTED_PROMPTS - {"orient"}))
def test_deep_dive_prompts_load(name: str) -> None:
    body = load_prompt(name)
    assert "{version}" not in body
    assert len(body) > 50, f"{name} prompt too thin; expected >=50 chars"
