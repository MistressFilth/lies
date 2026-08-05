"""Tests for wiki name validation."""

from __future__ import annotations

import pytest

from lies.errors import WikiNameError
from lies.wiki.validation import validate_name


@pytest.mark.parametrize(
    "name",
    ["mywiki", "research-2026", "team.alpha", "x", "a" * 200],
)
def test_validate_name_accepts_valid(name: str) -> None:
    validate_name(name)


@pytest.mark.parametrize(
    ("name", "reason_fragment"),
    [
        ("", "empty"),
        (".", "reserved"),
        ("..", "reserved"),
        ("foo/bar", "path separator"),
        ("foo\\bar", "path separator"),
        ("foo\x00bar", "null byte"),
        (".hidden", "leading dot"),
    ],
)
def test_validate_name_rejects_invalid(name: str, reason_fragment: str) -> None:
    with pytest.raises(WikiNameError) as exc:
        validate_name(name)
    assert reason_fragment in exc.value.reason
