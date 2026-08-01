"""Typed exception hierarchy for collections, scrapers, and etl packages."""

from lies.collections.errors import (
    CollectionConfigInvalid,
    CollectionError,
    CollectionNameRejected,
    CollectionNotFound,
)
from lies.etl.errors import (
    AtomicCommitFailed,
    BudgetExceeded,
    NormalizeError,
    PipelineError,
    QmdStale,
    SyncBusy,
    WriteError,
)
from lies.scrapers.errors import (
    ScraperError,
    ScraperFetchFailed,
    ScraperParseError,
    ScraperUnavailable,
)


def test_collection_hierarchy() -> None:
    assert issubclass(CollectionNotFound, CollectionError)
    assert issubclass(CollectionConfigInvalid, CollectionError)
    assert issubclass(CollectionNameRejected, CollectionConfigInvalid)
    assert CollectionNameRejected("x").path == "x"


def test_scraper_hierarchy() -> None:
    for cls in (ScraperUnavailable, ScraperFetchFailed, ScraperParseError):
        assert issubclass(cls, ScraperError)


def test_etl_hierarchy() -> None:
    for cls in (
        BudgetExceeded,
        NormalizeError,
        WriteError,
        QmdStale,
        AtomicCommitFailed,
        SyncBusy,
    ):
        assert issubclass(cls, PipelineError)


def test_budget_exceeded_carries_spent_and_cap() -> None:
    err = BudgetExceeded(spent=(2, 18_000), cap=(10, 500_000))
    assert err.spent == (2, 18_000)
    assert err.cap == (10, 500_000)


def test_sync_busy_carries_pid() -> None:
    err = SyncBusy(holding_pid=1234, wiki_root=None)
    assert err.holding_pid == 1234
