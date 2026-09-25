import pytest
from pathlib import Path
from lies.library.slug import derive_nested_slug, derive_slug, validate_slug


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("Getting_Started.md", "getting-started"),
        ("API-ref.rst", "api-ref"),
        ("page.html", "page"),
        ("multi.dot.name.md", "multi-dot-name"),
        ("UPPER.html", "upper"),
        ("trailing_.md", "trailing"),
    ],
)
def test_derive_slug_from_stem(tmp_path: Path, filename: str, expected: str) -> None:
    src = tmp_path / filename
    src.write_text("")
    assert derive_slug(src) == expected


def test_derive_slug_rejects_invalid_derived_stems(tmp_path: Path) -> None:
    """Minor 39: post-validation catches derived stems that don't match ``_VALID_RE``.

    The previous implementation passed the derived string straight through
    without re-validating, so a source named ``-leading.md`` (stem
    ``-leading``, starts with dash) or ``.dotfile`` (stem is the empty
    string after the leading dot) silently produced an invalid slug
    that would later crash ``write_mirror``. Re-validate after the
    derivation.
    """
    from lies.library.slug import SlugError

    bad_path = tmp_path / "-leading.md"
    bad_path.write_text("")
    with pytest.raises(SlugError, match="invalid slug"):
        derive_slug(bad_path)

    empty_path = Path(".")  # Path('.') has stem '' → derived is '' → invalid
    with pytest.raises(SlugError, match="invalid slug"):
        derive_slug(empty_path)


@pytest.mark.slow
def test_derive_slug_with_override(tmp_path: Path) -> None:
    src = tmp_path / "Original.html"
    src.write_text("")
    assert derive_slug(src, override="custom-name") == "custom-name"


@pytest.mark.parametrize("bad", ["", "../etc", "with space", "with.dot"])
def test_validate_slug_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_slug(bad)


def test_validate_slug_accepts_normal() -> None:
    assert validate_slug("normal-slug") == "normal-slug"
    assert validate_slug("with_under") == "with_under"


def test_validate_slug_accepts_nested_paths() -> None:
    """Nested paths (``<coll>/<file>``) are valid slugs.

    Used by the WebScraper when preserving source hierarchy under the
    collection root. Each segment must independently match the single
    segment regex; total length ≤1024 chars.
    """
    assert (
        validate_slug("agents-and-tools/agent-skills/best-practices")
        == "agents-and-tools/agent-skills/best-practices"
    )


def test_derive_nested_slug_strips_extension_and_normalizes() -> None:
    """Nested slug derivation: ``agents-and-tools/agent-skills/best-practices.md``
    becomes ``agents-and-tools/agent-skills/best-practices``."""
    assert (
        derive_nested_slug("agents-and-tools/agent-skills/best-practices.md")
        == "agents-and-tools/agent-skills/best-practices"
    )
    # Underscores and trailing dashes collapse.
    assert (
        derive_nested_slug("Build_With-Claude_/quick_start.md") == "build-with-claude/quick-start"
    )


@pytest.mark.parametrize(
    "source,expected",
    [
        # Dotted version tokens collapse to dashes (``3.14`` -> ``3-14``).
        # Repro: Python 3.16 docs include ``deprecations/
        # c-api-pending-removal-in-3.14.txt`` and similar. Without this,
        # the nested slug contains a ``.`` which the regex rejects and the
        # ingest quarantines the file.
        (
            "deprecations/c-api-pending-removal-in-3.14.txt",
            "deprecations/c-api-pending-removal-in-3-14",
        ),
        (
            "library/c-api-pending-removal-in-future.txt",
            "library/c-api-pending-removal-in-future",
        ),
        # Leading dashes stripped per segment. Repro: Python docs
        # ``library/__future__.txt`` ( ``__future__`` -> ``--future``
        # after underscore-to-dash ) must collapse to ``future`` so the
        # per-segment ``[a-z0-9]`` prefix requirement is met.
        ("library/__future__.txt", "library/future"),
        ("library/__thread__.txt", "library/thread"),
        ("library/__main__.txt", "library/main"),
        # Combined: both leading dashes and dotted tokens.
        (
            "deprecations/c-api-pending-removal-in-3-19.txt",
            "deprecations/c-api-pending-removal-in-3-19",
        ),
    ],
)
def test_derive_nested_slug_collapse_dots_and_leading_dashes(source: str, expected: str) -> None:
    """``derive_nested_slug`` collapses ``.`` and strips leading/trailing dashes
    per segment, matching what ``derive_slug`` does for the flat path.

    Without this, doc archives whose filenames include version numbers
    (Python docs, ``*.3.14.txt``) or dunder prefixes (Python docs,
    ``__future__.txt``) produce invalid slugs that the ingest pipeline
    quarantines one-by-one, aborting the run.
    """
    assert derive_nested_slug(source) == expected
