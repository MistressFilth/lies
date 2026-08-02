"""Typed errors raised by builders and the orchestrator.

All builders raise a subclass of :class:`BuilderError` so the
NORMALIZE stage can quarantine per-doc failures without
collapsing the whole sync.
"""

from __future__ import annotations


class BuilderError(Exception):
    """Base class for builder failures."""


class BuilderUnavailable(BuilderError):
    """No builder registered for the source_format."""

    def __init__(self, source_format: str) -> None:
        super().__init__(f"no builder registered for source_format={source_format!r}")
        self.source_format = source_format


class BuilderFetchFailed(BuilderError):
    """External tool (pandoc, pdfplumber, pymupdf) failed or absent."""

    def __init__(self, tool: str, message: str = "") -> None:
        if message:
            super().__init__(f"{tool} failed: {message}")
        else:
            super().__init__(f"{tool} failed")
        self.tool = tool


class BuilderParseError(BuilderError):
    """Source content is not parseable in the expected format."""

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path
