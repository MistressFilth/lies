"""Mirror file frontmatter: deterministic fields, no schema type."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Final

_BASE_DATE: Final[date] = date(2024, 1, 1)
_CAP_DATE: Final[date] = date(2099, 12, 31)
_BASE_ORDINAL: Final[int] = (_CAP_DATE - _BASE_DATE).days  # 27758
_HASH_SPACE: Final[int] = 1 << 32  # 4 bytes = uint32 max + 1


def _quote_yaml_string(value: str) -> str:
    """Double-quote a string for YAML output and escape ``\\`` and ``"``.

    Newlines (``\\r``/``\\n``) are stripped (replaced with a single space)
    so a hostile or accidental newline in a user-supplied field (notably
    ``title`` lifted from HTML/Markdown) cannot terminate the frontmatter
    block and inject a second top-level key. The same escape pattern is
    used by ``lies.page.author._format_author_body`` — kept consistent on
    purpose.
    """
    safe = value.replace("\r", " ").replace("\n", " ").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{safe}"'


def _ingested_at_from_hash(source_hash: str) -> str:
    """Map ``source_hash`` to an ISO date in ``[_BASE_DATE, _CAP_DATE]``.

    Deterministic: same hash always yields the same date. Saturates: the
    all-``f`` hash ``0xffffffff`` lands on ``_CAP_DATE`` exactly.

    The hash-suffix derivation reads ``source_hash[:8]`` (the first
    four bytes / eight hex chars of the upstream SHA256). The bound
    check enforces that prefix length — see Minor 35: the previous
    ``len(source_hash) < 4`` check was too permissive and let a 5-char
    partial hash through (only enough for the 4-byte ``int()`` parse
    to fail and fall back to ``_BASE_DATE``, silently discarding the
    ingest-date signal).
    """
    # Minor 35: spec mandates "first 4 bytes" (= 8 hex chars). The
    # strict-less-than-8 check ensures we never attempt a partial
    # ``int()`` parse on a hash that does not actually have the full
    # 8-hex-char prefix; otherwise the parse would silently fall back
    # to ``_BASE_DATE`` and a fresh re-ingest would lose its
    # deterministic ``ingested_at`` date.
    if not source_hash or len(source_hash) < 8:
        return _BASE_DATE.isoformat()
    try:
        first4 = int(source_hash[:8], 16)
    except ValueError:
        return _BASE_DATE.isoformat()
    # Linear map uint32 -> [0, _BASE_ORDINAL]. Multiplication keeps
    # ``0xffffffff`` saturated at the cap; a modulo would not, since
    # ``_BASE_ORDINAL + 1`` does not divide ``2**32``.
    days = first4 * (_BASE_ORDINAL + 1) // _HASH_SPACE
    return (_BASE_DATE + timedelta(days=days)).isoformat()


def build_frontmatter(
    *,
    title: str,
    source_url: str | None,
    source_path: str | None,
    source_hash: str,
    fetched_via: str,
) -> str:
    ingested_at = _ingested_at_from_hash(source_hash)
    lines = [
        "---",
        f"title: {_quote_yaml_string(title)}",
        f"source_url: {_quote_yaml_string(source_url) if source_url is not None else 'null'}",
        f"source_path: {_quote_yaml_string(source_path) if source_path is not None else 'null'}",
        f"source_hash: {source_hash}",
        f"fetched_via: {fetched_via}",
        f"ingested_at: {ingested_at}",
        "---",
    ]
    return "\n".join(lines) + "\n"


__all__ = ("build_frontmatter",)
