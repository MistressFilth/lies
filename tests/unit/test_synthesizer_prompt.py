"""Tests pinning the query_synthesizer agent prompt content.

The synthesizer prompt lives in
``src/lies/agents/query_synthesizer.py`` as the
``QUERY_SYNTHESIZER_SYSTEM_PROMPT`` module constant — not a YAML file
(as the task brief originally assumed). The tests below scan that
module for the source-rule text so a future refactor that moves the
prompt into YAML keeps the rule intact.
"""

from __future__ import annotations

from pathlib import Path

PROMPT_FILE = Path(
    "/home/divinefilth/code/github/MistressFilth/lies/mcp-query-library-as-primary/src/lies/agents/query_synthesizer.py"
)


def test_synthesizer_prompt_includes_source_rule() -> None:
    """The query_synthesizer agent prompt mentions the library-wins-on-
    conflict rule and the source tag convention."""
    text = PROMPT_FILE.read_text(encoding="utf-8")
    assert "library" in text.lower()
    assert "wiki" in text.lower()
    assert "primary source of truth" in text or "primary source" in text


def test_synthesizer_prompt_mentions_source_tags() -> None:
    """The query_synthesizer agent prompt names the `[library]` and
    `[wiki]` citation tags so the agent preserves them in answers."""
    text = PROMPT_FILE.read_text(encoding="utf-8")
    assert "[library]" in text
    assert "[wiki]" in text


def test_synthesizer_prompt_says_library_wins_on_conflict() -> None:
    """The query_synthesizer agent prompt instructs the agent to prefer
    library over wiki when sources contradict."""
    text = PROMPT_FILE.read_text(encoding="utf-8").lower()
    assert "contradict" in text
    assert "agree with library" in text
