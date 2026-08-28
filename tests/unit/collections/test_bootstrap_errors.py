"""Tests for the new bootstrap-related exceptions."""

from __future__ import annotations

from lies.collections.errors import (
    CollectionError,
    CollectionMismatch,
    WikiLayoutInitFailed,
    WizardAborted,
    WizardRequiresTTY,
)


def test_collection_mismatch_carries_both_sources() -> None:
    exc = CollectionMismatch(
        existing_source="https://old.example.com",
        existing_format=None,
        requested_source="https://new.example.com",
        requested_format=None,
    )
    assert isinstance(exc, CollectionError)
    assert exc.existing_source == "https://old.example.com"
    assert exc.requested_source == "https://new.example.com"
    assert "old.example.com" in str(exc)
    assert "new.example.com" in str(exc)


def test_wizard_aborted_subclasses_collection_error() -> None:
    exc = WizardAborted()
    assert isinstance(exc, CollectionError)
    assert "wizard" in str(exc).lower()


def test_wizard_requires_tty_subclasses_collection_error() -> None:
    exc = WizardRequiresTTY()
    assert isinstance(exc, CollectionError)
    assert "tty" in str(exc).lower()


def test_wiki_layout_init_failed_carries_underlying() -> None:
    cause = RuntimeError("disk full")
    exc = WikiLayoutInitFailed("mywiki", cause)
    assert isinstance(exc, CollectionError)
    assert exc.wiki_name == "mywiki"
    assert exc.__cause__ is cause
    assert "mywiki" in str(exc)
