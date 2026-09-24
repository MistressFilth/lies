"""Tests for the ReindexResult model returned by qmd_reindex."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lies.qmd._models import ReindexResult


def test_default_construction_all_false() -> None:
    """Defaults: all flags False, no errors."""
    r = ReindexResult()
    assert r.reconciled is False
    assert r.indexed is False
    assert r.embedded is False
    assert r.cleaned is False
    assert r.errors == []


def test_construction_with_all_flags() -> None:
    r = ReindexResult(reconciled=True, indexed=True, embedded=True, cleaned=True, errors=["x"])
    assert r.reconciled is True
    assert r.errors == ["x"]


def test_errors_must_be_list_of_strings() -> None:
    with pytest.raises(ValidationError):
        ReindexResult(errors=[1, 2])  # type: ignore[list-item]


def test_model_is_immutable_enough_to_dump() -> None:
    """model_dump returns dict with all fields."""
    r = ReindexResult(reconciled=True)
    d = r.model_dump()
    assert d == {
        "reconciled": True,
        "indexed": False,
        "embedded": False,
        "cleaned": False,
        "errors": [],
    }
