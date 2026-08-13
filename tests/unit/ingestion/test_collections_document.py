"""Tests for the Document record."""

from datetime import UTC, datetime

import pytest

from lies.collections.document import Document, DocumentStatus


def test_document_carries_sha256s() -> None:
    d = Document(
        path="concepts/example.md",
        source_sha256="a" * 64,
        ingested_sha256="b" * 64,
        ingested_at=datetime(2026, 8, 1, tzinfo=UTC),
        collection="cpython",
        status="ok",
    )
    assert d.source_sha256 == "a" * 64
    assert d.ingested_sha256 == "b" * 64


def test_document_status_enum() -> None:
    assert DocumentStatus.OK.value == "ok"
    assert DocumentStatus.QUARANTINED.value == "quarantined"
    assert DocumentStatus.SKIPPED.value == "skipped"


def test_document_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        Document(
            path="x.md",
            source_sha256="a" * 64,
            ingested_sha256="b" * 64,
            ingested_at=datetime.now(tz=UTC),
            collection="cpython",
            status="bogus",
        )
