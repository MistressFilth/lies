"""Slug derivation + validation for library mirror files."""

from __future__ import annotations

import re
from pathlib import Path

_VALID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


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
    if not derived or not _VALID_RE.fullmatch(derived):
        raise SlugError(f"derive_slug produced an invalid slug from {source!r}: {derived!r}")
    return derived


def validate_slug(slug: str) -> str:
    if not _VALID_RE.fullmatch(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    return slug


__all__ = ("SlugError", "derive_slug", "validate_slug")
