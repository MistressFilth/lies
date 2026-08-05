"""Tests for wiki-related errors."""

from __future__ import annotations

from pathlib import Path

from lies.errors import WikiAlreadyExists, WikiNameError, WikiNotRegistered


def test_wiki_already_exists_carries_fields() -> None:
    err = WikiAlreadyExists("mywiki", Path("/data/lies/mywiki"))
    assert err.name == "mywiki"
    assert err.existing_path == Path("/data/lies/mywiki")
    assert "mywiki" in str(err)
    assert "/data/lies/mywiki" in str(err)


def test_wiki_not_registered_carries_fields() -> None:
    err = WikiNotRegistered("missing", Path("/data/lies"))
    assert err.name == "missing"
    assert err.data_home == Path("/data/lies")
    assert "missing" in str(err)


def test_wiki_name_error_carries_reason() -> None:
    err = WikiNameError("foo/bar", reason="path separator")
    assert err.name == "foo/bar"
    assert "foo/bar" in str(err)
    assert "path separator" in str(err)
