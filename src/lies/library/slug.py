"""Slug derivation + validation for library mirror files."""

from __future__ import annotations

import re
from pathlib import Path

_VALID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


def derive_slug(source: Path, *, override: str | None = None) -> str:
    if override is not None:
        return validate_slug(override)
    return source.stem.lower().replace("_", "-").replace(".", "-").rstrip("-")


def validate_slug(slug: str) -> str:
    if not _VALID_RE.fullmatch(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    return slug


__all__ = ("derive_slug", "validate_slug")
