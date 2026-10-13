"""Slug derivation + validation for library mirror files."""

from __future__ import annotations

import re
from pathlib import Path

# Single-segment slug: ``^[a-z0-9][a-z0-9_-]{0,127}$``.
# Nested slug: one-or-more segments separated by ``/``, each segment
# matching the single-segment regex. Total path ≤1024 chars.
_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_NESTED_RE = re.compile(r"^(?:[a-z0-9][a-z0-9_-]{0,127})(?:/(?:[a-z0-9][a-z0-9_-]{0,127}))*$")


class SlugError(ValueError):
    """Raised when ``derive_slug`` cannot produce a valid slug.

    Subclasses ``ValueError`` so callers that catch ``ValueError``
    (e.g. ``ingest._process_item``'s ``except ValueError: quarantine``)
    still see the failure — but with a more specific type for
    diagnostics. See Minor 39.
    """


def derive_slug(source: Path, *, override: str | None = None) -> str:
    """Derive a valid slug from ``source``."""
    if override is not None:
        return validate_slug(override)
    derived = source.stem.lower().replace("_", "-").replace(".", "-").rstrip("-")
    # The derived stem can be empty (``Path("..")``, or ``Path(".dotfile")`` whose stem is
    # ""). Without post-validation it would pass through and the pipeline would write
    # ``<collection>/.md``, breaking the directory layout. Re-validate and raise ``SlugError``.
    if not derived or not _SEGMENT_RE.fullmatch(derived):
        raise SlugError(f"derive_slug produced an invalid slug from {source!r}: {derived!r}")
    return derived


def derive_nested_slug(source: Path | str) -> str:
    """Derive a slug that preserves the source's nested path structure."""
    raw = Path(source).as_posix()
    *parents, tail = raw.rsplit("/", 1)
    stem = tail.rsplit(".", 1)[0] if "." in tail else tail
    # ``rsplit("/", 1)`` yields at most two values, so ``parents`` is a single string holding
    # every remaining separator, not a list of segments. Split it before normalizing, or a
    # dotted or underscored *interior* directory keeps the leading dash its own
    # normalization produced and the whole slug fails validation.
    segments = [seg for parent in parents for seg in parent.split("/")]
    normalized_parts = [
        # ``.txt`` / version-like tokens (``3.14``) inside source paths must collapse to dashes —
        # the nested regex accepts only ``[a-z0-9_-]``, and ``derive_slug`` already does this for
        # the flat path. Leading dashes are stripped so ``__future__`` still matches.
        seg.lower().replace("_", "-").replace(".", "-").strip("-")
        for seg in segments + [stem]
        if seg
    ]
    if not normalized_parts or any(not p for p in normalized_parts):
        raise SlugError(f"derive_nested_slug produced an invalid slug from {source!r}")
    derived = "/".join(normalized_parts)
    return validate_slug(derived)


def validate_slug(slug: str) -> str:
    if not _NESTED_RE.fullmatch(slug) or len(slug) > 1024:
        raise ValueError(f"invalid slug: {slug!r}")
    return slug


__all__ = ("SlugError", "derive_slug", "derive_nested_slug", "validate_slug")
