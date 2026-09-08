"""Mirror file frontmatter: deterministic fields, no schema type."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict

_BASE_DATE: Final[date] = date(2024, 1, 1)
_CAP_DATE: Final[date] = date(2099, 12, 31)
_BASE_ORDINAL: Final[int] = (_CAP_DATE - _BASE_DATE).days  # 27758
_HASH_SPACE: Final[int] = 1 << 32  # 4 bytes = uint32 max + 1


class MirrorFrontmatter(BaseModel):
    """Deterministic mirror frontmatter schema (no ``type:`` field).

    Mirrors are library-internal artifacts, not wiki pages, so they do not
    carry a schema ``type:`` tag. ``ingested_at`` is derived from
    ``source_hash`` (see :func:`build_frontmatter`) so re-ingest yields
    byte-identical YAML.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    source_url: str | None
    source_path: str | None
    source_hash: str
    fetched_via: str
    ingested_at: str


def _ingested_at_from_hash(source_hash: str) -> str:
    """Map ``source_hash`` to an ISO date in ``[_BASE_DATE, _CAP_DATE]``.

    Deterministic: same hash always yields the same date. Saturates: the
    all-``f`` hash ``0xffffffff`` lands on ``_CAP_DATE`` exactly.
    """
    if not source_hash or len(source_hash) < 4:
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
        f"title: {title}",
        f"source_url: {source_url if source_url is not None else 'null'}",
        f"source_path: {source_path if source_path is not None else 'null'}",
        f"source_hash: {source_hash}",
        f"fetched_via: {fetched_via}",
        f"ingested_at: {ingested_at}",
        "---",
    ]
    return "\n".join(lines) + "\n"


__all__ = ("MirrorFrontmatter", "build_frontmatter")
