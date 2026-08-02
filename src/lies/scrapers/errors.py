"""Typed exceptions for the scrapers package."""

from __future__ import annotations


class ScraperError(Exception):
    """Base class for scraper-level errors."""


class ScraperUnavailable(ScraperError):
    """Required tool (e.g., playwright) is missing."""


class ScraperFetchFailed(ScraperError):
    """HTTP/clone/fetch step failed."""


class ScraperParseError(ScraperError):
    """Parsing the fetched bytes into docs failed."""
