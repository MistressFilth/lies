"""Unit tests for ``lies.query.format_validator``.

The validator silently demotes ``table`` / ``marp`` hints to ``md`` when
the body fails to parse for that format. The contract is pure: the
input body is never mutated, and the only signal back to the caller is
the resolved hint.
"""

from __future__ import annotations

from lies.query.format_validator import (
    _strip_fenced,
    _validate_marp,
    _validate_md,
    _validate_table,
    validate_format,
)


# ---------------------------------------------------------------------------
# _validate_md — trivial
# ---------------------------------------------------------------------------


def test_validate_md_trivially_true() -> None:
    assert _validate_md("anything") is True
    assert _validate_md("") is True
    assert _validate_md("```\ncode block\n```") is True


def test_validate_md_empty_body() -> None:
    assert _validate_md("") is True


# ---------------------------------------------------------------------------
# _validate_table
# ---------------------------------------------------------------------------


def test_validate_table_well_formed() -> None:
    body = "| col1 | col2 |\n| --- | --- |\n| a | b |\n| c | d |\n"
    assert _validate_table(body) is True


def test_validate_table_missing_separator() -> None:
    body = "| col1 | col2 |\n| a | b |\n"
    assert _validate_table(body) is False


def test_validate_table_partial_no_data_rows() -> None:
    body = "| col1 | col2 |\n| --- | --- |\n"
    assert _validate_table(body) is False


def test_validate_table_unbalanced_pipes() -> None:
    body = (
        "| col1 | col2\n"  # missing closing pipe on header
        "| --- | --- |\n"
        "| a | b |\n"
    )
    assert _validate_table(body) is False


def test_validate_table_pipe_inside_code_block_ignored() -> None:
    body = (
        "intro paragraph\n"
        "```\n"
        "| not | a | table |\n"
        "| --- | --- | --- |\n"
        "| x | y | z |\n"
        "```\n"
        "\n"
        "| col1 | col2 |\n"
        "| --- | --- |\n"
        "| a | b |\n"
    )
    assert _validate_table(body) is True


def test_validate_table_empty_body() -> None:
    assert _validate_table("") is False


def test_validate_table_malformed_separator_with_extra_dashes() -> None:
    # Separator row that is not on the line immediately following the
    # header is rejected — intervening non-blank lines break the table.
    body = "| col1 | col2 |\nintervening prose\n| --- | --- |\n| a | b |\n"
    assert _validate_table(body) is False


def test_validate_table_single_column_rows() -> None:
    # Single-column GFM tables are valid GFM and the validator should
    # accept them.
    body = "| col1 |\n| --- |\n| a |\n| b |\n"
    assert _validate_table(body) is True


def test_validate_table_multiple_tables_only_second_valid() -> None:
    # First "table" is malformed (missing separator); second is well-formed.
    body = (
        "| col1 | col2 |\n"
        "| a | b |\n"  # not a separator
        "\n"
        "| col3 | col4 |\n"
        "| --- | --- |\n"
        "| x | y |\n"
    )
    assert _validate_table(body) is True


def test_validate_table_embedded_in_list() -> None:
    # A table inside a list item: the first non-blank line after the
    # header is the separator, so this counts as a table.
    body = "- here is a table:\n\n  | col1 | col2 |\n  | --- | --- |\n  | a | b |\n"
    assert _validate_table(body) is True


# ---------------------------------------------------------------------------
# _validate_marp
# ---------------------------------------------------------------------------


def test_validate_marp_with_frontmatter_and_slide_break() -> None:
    body = (
        "---\nmarp: true\ntheme: default\n---\n\n# Slide 1\n\ncontent\n\n---\n\n# Slide 2\n\nmore\n"
    )
    assert _validate_marp(body) is True


def test_validate_marp_missing_frontmatter() -> None:
    body = "# Slide 1\n\n---\n\n# Slide 2\n"
    assert _validate_marp(body) is False


def test_validate_marp_frontmatter_but_no_slide_break() -> None:
    body = "---\nmarp: true\n---\n\n# Single slide\n"
    assert _validate_marp(body) is False


def test_validate_marp_slide_break_inside_code_block_ignored() -> None:
    body = "---\nmarp: true\n---\n\n# Slide 1\n\n```\nliteral ---\n```\n"
    assert _validate_marp(body) is False  # no real slide break


def test_validate_marp_empty_body() -> None:
    assert _validate_marp("") is False


def test_validate_marp_yes_directive() -> None:
    # `marp: yes` is the YAML-truthy variant of `marp: true`.
    body = "---\nmarp: yes\n---\n\n# Slide 1\n\n---\n\n# Slide 2\n"
    assert _validate_marp(body) is True


def test_validate_marp_slide_break_inside_tilde_fence() -> None:
    body = "---\nmarp: true\n---\n\n# Slide 1\n\n~~~\nliteral ---\n~~~\n"
    assert _validate_marp(body) is False  # tilde-fenced --- not a slide break


def test_validate_marp_just_three_dashes() -> None:
    # A body that is exactly "---" is a thematic break, not a marp deck.
    assert _validate_marp("---") is False


def test_validate_marp_two_consecutive_separators_only_one_is_slide_break() -> None:
    # Frontmatter `---` is a fence, not a slide break. The body must
    # additionally contain at least one `---` line outside the
    # frontmatter block. The second `---` here is the frontmatter
    # closer; the third is a real slide break; the fourth is a
    # thematic break inside prose (still counts as a slide break
    # because the validator doesn't track thematic-break vs slide-break
    # semantics — `---` lines outside fences all count).
    body = "---\nmarp: true\n---\n\n# Slide 1\n\n---\n\nparagraph\n\n---\n"
    assert _validate_marp(body) is True


# ---------------------------------------------------------------------------
# _strip_fenced
# ---------------------------------------------------------------------------


def test_strip_fenced_removes_code_blocks() -> None:
    body = "before\n```\ncode\n```\nafter"
    assert _strip_fenced(body) == "before\n\nafter"


def test_strip_fenced_handles_tilde_fences() -> None:
    body = "before\n~~~\ncode\n~~~\nafter"
    assert _strip_fenced(body) == "before\n\nafter"


# ---------------------------------------------------------------------------
# validate_format — public dispatch
# ---------------------------------------------------------------------------


def test_validate_format_returns_hint_on_success() -> None:
    body = "| a | b |\n| --- | --- |\n| c | d |\n"
    assert validate_format(body, "table") == "table"


def test_validate_format_demotes_table_to_md_on_failure() -> None:
    body = "just bullets\n- one\n- two\n"
    assert validate_format(body, "table") == "md"


def test_validate_format_keeps_body_unmodified() -> None:
    body = "just bullets\n- one\n- two\n"
    validate_format(body, "table")
    assert body == "just bullets\n- one\n- two\n"


def test_validate_format_marp_happy() -> None:
    body = "---\nmarp: true\n---\n\n# S1\n\n---\n\n# S2\n"
    assert validate_format(body, "marp") == "marp"


def test_validate_format_marp_demotes() -> None:
    body = "# Just markdown\n\nNo marp here.\n"
    assert validate_format(body, "marp") == "md"


def test_validate_format_md_hint_is_passthrough() -> None:
    # md has no validator; any body is fine, hint stays "md".
    assert validate_format("anything at all", "md") == "md"
    assert validate_format("", "md") == "md"


def test_validate_format_whitespace_only_body() -> None:
    # Whitespace-only body demotes table and marp; passes through md.
    whitespace = "   \n\n  \n"
    assert validate_format(whitespace, "table") == "md"
    assert validate_format(whitespace, "marp") == "md"
    assert validate_format(whitespace, "md") == "md"
