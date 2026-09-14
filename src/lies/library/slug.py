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
    """Derive a valid slug from ``source``.

    When ``override`` is set, the operator's choice is validated and
    returned (no derivation).

    Otherwise the bare ``source.stem`` is normalized: underscores and
    dots collapse to dashes and trailing dashes are stripped. The
    derived result is then re-validated through :func:`validate_slug`.
    A pathological input like ``Path(".dotfile")`` (stem is empty
    after the ``.dotfile`` → ``-dotfile`` → ``dotfile`` chain — but a
    source named ``..`` has an empty stem) returns an empty string
    BEFORE validation would accept it. The post-validation guard
    catches that case (Minor 39) and raises :class:`SlugError` so the
    caller can quarantine the doc with a typed reason.
    """
    if override is not None:
        return validate_slug(override)
    derived = source.stem.lower().replace("_", "-").replace(".", "-").rstrip("-")
    # Minor 39: the derived stem can be empty (e.g. ``Path("..")``,
    # ``Path(".dotfile")`` whose stem is the empty string). Without
    # the post-validation, the empty string would pass through and
    # the ingest pipeline would write ``<collection>/.md`` — a path
    # that breaks the collection's directory layout. Re-validate and
    # raise a typed SlugError.
    if not derived or not _SEGMENT_RE.fullmatch(derived):
        raise SlugError(f"derive_slug produced an invalid slug from {source!r}: {derived!r}")
    return derived


def derive_nested_slug(source: Path | str) -> str:
    """Derive a slug that preserves the source's nested path structure.

    Used for source-relative paths coming from a ``WebScraper`` URL
    hierarchy (e.g. ``agents-and-tools/agent-skills/best-practices.md``)
    where the caller wants the slug to mirror the upstream structure
    under the collection root. Local absolute paths from bootstrap
    (``/tmp/claude_platform/docs/foo.md``) are NOT appropriate — those
    callers should keep using :func:`derive_slug`, which flattens to a
    single segment.

    Each path segment is normalized: underscores and dots collapse to
    dashes, trailing dashes stripped. The result is validated by
    :func:`validate_slug` and may contain ``/`` separators between
    segments.
    """
    raw = Path(source).as_posix()
    *parents, tail = raw.rsplit("/", 1)
    stem = tail.rsplit(".", 1)[0] if "." in tail else tail
    normalized_parts = [
        seg.lower().replace("_", "-").rstrip("-") for seg in parents + [stem] if seg
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
