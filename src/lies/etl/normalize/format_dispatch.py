"""Format-aware markdown dispatcher."""
from __future__ import annotations


class UnknownFormatError(ValueError):
    """source_format is not recognized."""


def _pandoc_convert(raw: bytes, fmt: str) -> bytes:
    from lies.etl.normalize.pandoc_daemon import PandocDaemon  # type: ignore[import-untyped]
    with PandocDaemon() as d:
        return d.convert(raw, fmt)  # type: ignore[no-any-return]


def _pdf_extract(raw: bytes) -> str:
    from lies.etl.normalize.pdf import extract_text
    return extract_text(raw)


def dispatch(raw: bytes, source_format: str) -> str:
    fmt = source_format.lower()
    if fmt in ("markdown", "md", "mdx"):
        return raw.decode("utf-8", errors="replace")
    if fmt in ("html", "htm", "rst"):
        return _pandoc_convert(raw, fmt).decode("utf-8", errors="replace")
    if fmt == "pdf":
        return _pdf_extract(raw)
    if fmt == "liquid":
        # Build pipeline TBD per sister spec; quarantine for now.
        raise UnknownFormatError("liquid parsing not yet supported (deferred)")
    raise UnknownFormatError(f"unknown source format: {source_format!r}")
