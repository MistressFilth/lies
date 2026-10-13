"""Tests pinning the query_synthesizer agent prompt content."""

from __future__ import annotations

from pathlib import Path

import lies.agents.query_synthesizer as _prompt_module

PROMPT_FILE = Path(_prompt_module.__file__).resolve()


def test_synthesizer_prompt_includes_source_rule() -> None:
    """The prompt names the library-wins-on-conflict rule and the source tag convention."""
    text = PROMPT_FILE.read_text(encoding="utf-8")
    assert "library" in text.lower()
    assert "wiki" in text.lower()
    assert "primary source of truth" in text or "primary source" in text


def test_synthesizer_prompt_mentions_source_tags() -> None:
    """The query_synthesizer agent prompt names the `[library]` and `[wiki]` citation tags so the agent preserves them in answers."""
    text = PROMPT_FILE.read_text(encoding="utf-8")
    assert "[library]" in text
    assert "[wiki]" in text


def test_synthesizer_prompt_says_library_wins_on_conflict() -> None:
    """The query_synthesizer agent prompt instructs the agent to prefer library over wiki when sources contradict."""
    text = PROMPT_FILE.read_text(encoding="utf-8").lower()
    assert "contradict" in text
    assert "agree with library" in text
